from __future__ import annotations

import json
from pathlib import Path

from pkm_brain.db import connection
from pkm_brain.paths import BrainPaths
from pkm_brain.service import BrainService
from pkm_brain.wiki_curation_promote import promote_wiki_curation
from pkm_brain.wiki_fact_migration import extract_fact_statements
from pkm_brain.wiki_facts import (
    answer_open_question,
    apply_fact_status_action,
    archive_orphan_managed_pages,
    compact_statement,
    create_confirmed_page_fact,
    duplicate_fact_projection_errors,
    facts_directly_conflict,
    managed_fact_page_review,
    render_managed_page,
    regenerate_managed_fact_page,
    resolve_fact_groups,
    revert_wiki_page_snapshot,
    upsert_candidate_facts,
    wiki_fact_dashboard,
)


def test_facts_directly_conflict_requires_shared_topic() -> None:
    assert facts_directly_conflict(
        {"statement": "AlphaPay auto-renewal is enabled by default for annual plans."},
        {
            "statement": (
                "AlphaPay auto-renewal is not enabled by default for annual plans."
            )
        },
    )
    assert not facts_directly_conflict(
        {
            "statement": (
                "Before Catch, Peter was at Elation for six and a half years."
            )
        },
        {
            "statement": (
                "After the V2 redesign, engagement for the sample data exploration "
                "feature improved to about 10%."
            )
        },
    )
    assert not facts_directly_conflict(
        {
            "statement": (
                "The interviewer said Hightouch starts pulling data from the warehouse "
                "to Meta immediately after a sync is finalized."
            )
        },
        {
            "statement": (
                "Josh said Hightouch is targeting roughly doubling the business this "
                "year after reaching the $100M ARR milestone."
            )
        },
    )


def test_wiki_fact_dashboard_surfaces_needs_human_questions(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()
    with connection(paths.sqlite_path) as conn:
        conn.execute(
            """
            INSERT INTO open_questions(
              id, kind, entity_key, page_hint, fact_ids, question, options,
              status, context, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "question_review",
                "fact_conflict_review",
                "companies:sierra",
                "companies/sierra.md",
                json.dumps(["fact_candidate", "fact_existing"]),
                "Candidate appears to contradict an existing nearby fact.",
                "[]",
                "needs_human",
                "{}",
                "2026-07-07T00:00:00+00:00",
            ),
        )

    dashboard = wiki_fact_dashboard(paths)

    assert dashboard["counts"]["questions_by_status"]["needs_human"] == 1
    assert [question["id"] for question in dashboard["open_questions"]] == [
        "question_review"
    ]


def test_promote_wiki_curation_promotes_fact_state(tmp_path: Path) -> None:
    source = BrainPaths.from_value(tmp_path / "source")
    target = BrainPaths.from_value(tmp_path / "target")
    BrainService(source).init_workspace()
    BrainService(target).init_workspace()
    with connection(source.sqlite_path) as conn:
        conn.execute(
            """
            INSERT INTO facts(
              id, statement, entity_key, page_hint, section_hint, source_ids,
              observed_at, confidence, status, supersedes_id, conflict_group_id,
              confirmed_by_user, metadata, created_at, last_seen_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "fact_promoted",
                "Promoted curation fact.",
                "projects:promoted:summary",
                "projects/promoted.md",
                "Summary",
                json.dumps(["document:doc_test"]),
                "2026-06-23T00:00:00+00:00",
                0.9,
                "active",
                None,
                None,
                1,
                "{}",
                "2026-06-23T00:00:00+00:00",
                None,
            ),
        )
        conn.execute(
            """
            INSERT INTO open_questions(
              id, kind, entity_key, page_hint, fact_ids, question, options,
              status, answer, context, created_at, answered_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "question_promoted",
                "conflict",
                "projects:promoted:summary",
                "projects/promoted.md",
                json.dumps(["fact_promoted"]),
                "Which fact is correct?",
                "[]",
                "answered",
                "fact_promoted",
                "{}",
                "2026-06-23T00:00:00+00:00",
                "2026-06-23T01:00:00+00:00",
            ),
        )
        conn.execute(
            """
            INSERT INTO wiki_curation_runs(id, source_packet_id, group_by, status, summary, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            ("run_promoted", None, "topic", "ok", "{}", "2026-06-23T00:00:00+00:00"),
        )
        conn.execute(
            """
            INSERT INTO wiki_pages(
              id, title, page_type, status, path, source_ids, related, tags,
              created_at, updated_at, managed, fact_ids
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "managed-promoted",
                "Promoted",
                "project",
                "active",
                "projects/promoted.md",
                "[]",
                "[]",
                json.dumps(["managed"]),
                "2026-06-23",
                "2026-06-23",
                1,
                json.dumps(["fact_promoted"]),
            ),
        )
    source_page = source.wiki / "projects" / "promoted.md"
    source_page.parent.mkdir(parents=True)
    source_page.write_text(
        "---\n"
        "title: Promoted\n"
        "page_type: project\n"
        "id: managed-promoted\n"
        "status: active\n"
        "managed: true\n"
        "source_ids: []\n"
        "related: []\n"
        "tags:\n"
        "  - managed\n"
        "---\n\n"
        "# Promoted\n\n"
        "## Summary\n\nPromoted curation fact.\n",
        encoding="utf-8",
    )

    dry_run = promote_wiki_curation(source, target, dry_run=True)
    result = promote_wiki_curation(source, target, dry_run=False, backup=False)

    assert dry_run["source_counts"]["facts"] == 1
    assert dry_run["managed_wiki_files"] == 1
    assert result["applied"] is True
    assert (target.wiki / "projects" / "promoted.md").read_text(encoding="utf-8").count("Promoted curation fact") == 1
    with connection(target.sqlite_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == 1
        assert conn.execute("SELECT status FROM open_questions WHERE id = 'question_promoted'").fetchone()[0] == "answered"
        page = conn.execute("SELECT managed, fact_ids FROM wiki_pages WHERE id = 'managed-promoted'").fetchone()
    assert page["managed"] == 1
    assert json.loads(page["fact_ids"]) == ["fact_promoted"]


def test_managed_page_routes_each_fact_to_one_section() -> None:
    facts = [
        {
            "id": "fact_summary",
            "statement": "CloudZero sharing should be framed as role-aware collaboration, not broad dashboard distribution.",
            "section_hint": "Summary",
            "source_ids": ["document:doc_cloudzero"],
            "observed_at": "2026-05-30T08:03:04+00:00",
            "created_at": "2026-05-30T08:03:04+00:00",
        },
        {
            "id": "fact_definition",
            "statement": "A useful access model distinguishes inherited identity groups from custom FinOps administrator groups.",
            "section_hint": "Definition",
            "source_ids": ["document:doc_cloudzero"],
            "observed_at": "2026-05-30T09:03:04+00:00",
            "created_at": "2026-05-30T09:03:04+00:00",
        },
        {
            "id": "fact_why",
            "statement": "This matters because cross-functional leaders need curated cost visibility without full administrative control.",
            "section_hint": "Why It Matters",
            "source_ids": ["document:doc_cloudzero"],
            "observed_at": "2026-05-30T10:03:04+00:00",
            "created_at": "2026-05-30T10:03:04+00:00",
        },
    ]

    markdown = render_managed_page("concepts/cloudzero-sharing.md", facts)

    assert duplicate_fact_projection_errors("concepts/cloudzero-sharing.md", markdown) == []
    assert markdown.count("role-aware collaboration") == 1
    assert markdown.count("inherited identity groups") == 1
    assert markdown.count("curated cost visibility") == 1
    assert "## Summary\n\nThis managed page is maintained from 3 active facts across 1 source." in markdown


def test_managed_page_can_include_optional_derived_synthesis_block() -> None:
    facts = [
        {
            "id": "fact_synthesis",
            "statement": "Derived synthesis must cite fact IDs and remain non-canonical.",
            "section_hint": "Summary",
            "source_ids": ["document:doc_synthesis"],
            "observed_at": "2026-05-30T08:03:04+00:00",
            "created_at": "2026-05-30T08:03:04+00:00",
        }
    ]

    with_synthesis = render_managed_page(
        "concepts/synthesis.md",
        facts,
        synthesis_markdown="- Synthesis from [fact_synthesis].",
    )
    canonical_only = render_managed_page("concepts/synthesis.md", facts)

    assert "## Derived Synthesis" in with_synthesis
    assert "fact_synthesis" in with_synthesis
    assert "## Derived Synthesis" not in canonical_only
    assert "Derived synthesis must cite fact IDs" in canonical_only


def test_managed_page_suppresses_near_duplicate_facts_across_sections() -> None:
    facts = [
        {
            "id": "fact_old",
            "statement": "The Hightouch interview introduced an alternative PM role focused on a major AI and data initiative around the CDP experience.",
            "section_hint": "Why It Matters",
            "source_ids": ["document:doc_hightouch"],
            "confidence": 0.7,
            "observed_at": "2026-05-29T08:00:00+00:00",
            "created_at": "2026-05-29T08:00:00+00:00",
        },
        {
            "id": "fact_new",
            "statement": "During the May 29, 2026 Hightouch interview process, the team introduced an alternate PM opportunity focused on a major AI and data initiative around the CDP experience.",
            "section_hint": "How It Works",
            "source_ids": ["document:doc_hightouch"],
            "confidence": 0.86,
            "observed_at": "2026-05-30T08:00:00+00:00",
            "created_at": "2026-05-30T08:00:00+00:00",
        },
    ]

    markdown = render_managed_page("concepts/hightouch.md", facts)

    assert "fact_old" in markdown
    assert "fact_new" in markdown
    assert markdown.count("major AI and data initiative around the CDP experience") == 1
    assert duplicate_fact_projection_errors("concepts/hightouch.md", markdown) == []


def test_compact_statement_preserves_legacy_inline_heading_prefixed_facts() -> None:
    statement = compact_statement(
        "## May 2026 Interview Notes ### Product Vision Discussed "
        "In the Hightouch process, Peter framed a staged evolution from campaign intelligence to agentic lifecycle marketing."
    )

    assert statement
    assert "##" not in statement
    assert "###" not in statement
    assert "Hightouch process" in statement


def test_migration_fact_extraction_cleans_markdown_noise() -> None:
    statements = extract_fact_statements(
        "### Product Vision\n\n"
        "- **Trust:** Full autonomy should come after safe recommendations are proven.\n\n"
        "A staged workflow moves from analysis to recommendations to drafted experiments."
    )

    assert statements == [
        "Trust: Full autonomy should come after safe recommendations are proven.",
        "A staged workflow moves from analysis to recommendations to drafted experiments.",
    ]
    assert all("#" not in statement for statement in statements)


def test_candidate_fact_upsert_records_cos_action(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()

    result = upsert_candidate_facts(
        paths,
        [
            {
                "statement": "The migration path records facts through the action ledger.",
                "entity_key": "concepts:test-ledger:summary",
                "page_hint": "concepts/test-ledger.md",
                "section_hint": "Summary",
                "source_ids": ["document:doc_ledger"],
                "observed_at": "2026-06-25T00:00:00+00:00",
                "confidence": 0.82,
                "metadata": {"migration": "test"},
            }
        ],
    )

    assert len(result["created_fact_ids"]) == 1
    with connection(paths.sqlite_path) as conn:
        action = conn.execute(
            "SELECT * FROM cos_actions WHERE action_type = 'fact_upsert'"
        ).fetchone()
        fact_count = conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0]

    assert fact_count == 1
    assert action is not None
    assert action["status"] == "applied"
    assert action["proposed_by"] == "wiki_fact_migration"


def test_orphan_managed_page_is_archived_without_old_fact_bullets(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()
    page = paths.wiki / "concepts" / "stale.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(
        "---\n"
        "title: Stale\n"
        "page_type: concept\n"
        "id: concept-stale\n"
        "status: active\n"
        "source_ids:\n"
        "  - document:doc_old\n"
        "related: []\n"
        "tags:\n"
        "  - managed\n"
        "managed: true\n"
        "fact_ids:\n"
        "  - fact_old\n"
        "---\n\n"
        "<!-- generated-by: pkm-brain chief-of-staff-facts v1 -->\n\n"
        "# Stale\n\n"
        "## Summary\n\n- The same old fact.\n\n"
        "## Key Points\n\n- The same old fact.\n",
        encoding="utf-8",
    )

    archived = archive_orphan_managed_pages(paths, active_page_hints=[])
    markdown = page.read_text(encoding="utf-8")

    assert archived[0]["relative_path"] == "concepts/stale.md"
    assert archived[0]["path"] == str(page)
    assert archived[0]["status"] == "archived"
    assert archived[0]["snapshot_id"].startswith("wikisnap_")
    assert archived[0]["action_id"].startswith("cosact_")
    assert "status: archived" in markdown
    assert "fact_ids: []" in markdown
    assert "The same old fact" not in markdown
    assert duplicate_fact_projection_errors("concepts/stale.md", markdown) == []


def test_managed_page_review_correction_snapshots_and_revert(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()
    with connection(paths.sqlite_path) as conn:
        conn.execute(
            """
            INSERT INTO facts(
              id, statement, entity_key, page_hint, section_hint, source_ids,
              observed_at, confidence, status, confirmed_by_user, metadata,
              created_at, last_seen_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "fact_original",
                "The managed review page starts with the original fact.",
                "concepts:managed-review:summary",
                "concepts/managed-review.md",
                "Summary",
                json.dumps(["document:doc_source"]),
                "2026-06-23T00:00:00+00:00",
                0.8,
                "active",
                0,
                "{}",
                "2026-06-23T00:00:00+00:00",
                None,
            ),
        )

    generated = regenerate_managed_fact_page(
        paths, "concepts/managed-review.md", dry_run=False
    )
    page = paths.wiki / "concepts" / "managed-review.md"
    assert generated["curation"]["pages"][0]["snapshot_id"].startswith("wikisnap_")
    assert "original fact" in page.read_text(encoding="utf-8")

    corrected = create_confirmed_page_fact(
        paths,
        "concepts/managed-review.md",
        "The managed review page now uses the corrected fact.",
        supersede_fact_ids=["fact_original"],
    )
    assert corrected["fact"]["confirmed_by_user"] is True
    assert corrected["action"]["action_type"] == "fact_upsert"
    assert corrected["action"]["status"] == "applied"
    assert corrected["action"]["target_fact_ids"] == [corrected["fact"]["id"]]
    assert corrected["supersede_action"]["action_type"] == "fact_supersede"
    assert corrected["supersede_action"]["status"] == "applied"
    corrected_markdown = page.read_text(encoding="utf-8")
    assert "corrected fact" in corrected_markdown
    assert "original fact" not in corrected_markdown

    review = managed_fact_page_review(paths, "concepts/managed-review.md")
    correction_snapshot_id = corrected["curation"]["pages"][0]["snapshot_id"]
    assert correction_snapshot_id in {snapshot["id"] for snapshot in review["snapshots"]}
    reverted = revert_wiki_page_snapshot(paths, correction_snapshot_id)

    assert reverted["revert_snapshot_id"].startswith("wikisnap_")
    assert reverted["action"]["action_type"] == "revert_page_snapshot"
    assert reverted["action"]["status"] == "applied"
    reverted_markdown = page.read_text(encoding="utf-8")
    assert "original fact" in reverted_markdown
    assert "corrected fact" not in reverted_markdown


def test_resolve_fact_groups_records_display_contested_action(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()
    with connection(paths.sqlite_path) as conn:
        for fact_id, statement in [
            ("fact_conflict_left", "The test service uses Postgres for storage."),
            ("fact_conflict_right", "The test service uses MongoDB for storage."),
        ]:
            conn.execute(
                """
                INSERT INTO facts(
                  id, statement, entity_key, page_hint, section_hint, source_ids,
                  observed_at, confidence, status, metadata, created_at, last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    fact_id,
                    statement,
                    "concepts:test-service:summary",
                    "concepts/test-service.md",
                    "Summary",
                    json.dumps([f"document:{fact_id}"]),
                    "2026-06-23T00:00:00+00:00",
                    0.6,
                    "active",
                    json.dumps({"operation": "replace_page"}),
                    "2026-06-23T00:00:00+00:00",
                    None,
                ),
            )

    result = resolve_fact_groups(paths, ["concepts:test-service:summary"])

    assert len(result["created_question_ids"]) == 1
    with connection(paths.sqlite_path) as conn:
        action = conn.execute(
            "SELECT * FROM cos_actions WHERE action_type = 'display_contested'"
        ).fetchone()
        question = conn.execute(
            "SELECT * FROM open_questions WHERE id = ?",
            (result["created_question_ids"][0],),
        ).fetchone()
        statuses = {
            row["id"]: row["status"]
            for row in conn.execute(
                "SELECT id, status FROM facts WHERE id IN ('fact_conflict_left', 'fact_conflict_right')"
            )
        }

    assert action is not None
    assert action["status"] == "applied"
    assert question["action_id"] == action["id"]
    assert statuses == {
        "fact_conflict_left": "conflicted",
        "fact_conflict_right": "conflicted",
    }


def test_fact_status_action_skips_exact_noop_updates(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()
    with connection(paths.sqlite_path) as conn:
        conn.execute(
            """
            INSERT INTO facts(
              id, statement, entity_key, page_hint, section_hint, source_ids,
              observed_at, confidence, status, supersedes_id, conflict_group_id,
              confirmed_by_user, metadata, created_at, last_seen_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "fact_already_active",
                "The already-active fact should not create a resolver action.",
                "concepts:test-service:summary",
                "concepts/test-service.md",
                "Summary",
                json.dumps(["document:fact_already_active"]),
                "2026-06-23T00:00:00+00:00",
                0.8,
                "active",
                None,
                None,
                0,
                "{}",
                "2026-06-23T00:00:00+00:00",
                None,
            ),
        )

    result = apply_fact_status_action(
        paths,
        "fact_supersede",
        [
            {
                "fact_id": "fact_already_active",
                "status": "active",
                "conflict_group_id": None,
            }
        ],
        proposed_by="resolve_fact_groups",
        risk_tier="low",
    )

    assert result["status"] == "skipped"
    assert result["reason"] == "no_fact_status_changes"
    with connection(paths.sqlite_path) as conn:
        action_count = conn.execute("SELECT COUNT(*) FROM cos_actions").fetchone()[0]
    assert action_count == 0


def test_resolver_llm_can_merge_same_claim_replacements(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()
    insert_replacement_fact(
        paths,
        "fact_resolver_left",
        "AlphaPay retry billing uses Stripe Checkout for renewal invoices.",
    )
    insert_replacement_fact(
        paths,
        "fact_resolver_right",
        "AlphaPay payment retry uses Stripe Checkout for renewal invoices.",
    )

    result = resolve_fact_groups(
        paths,
        ["concepts:resolver-test:summary"],
        llm_provider=FakeResolverProvider("same_claim"),
    )

    assert result["resolver_judgment_count"] == 1
    assert result["auto_merged"] == 1
    with connection(paths.sqlite_path) as conn:
        action = conn.execute("SELECT * FROM cos_actions WHERE action_type = 'fact_merge'").fetchone()
        statuses = {
            row["id"]: row["status"]
            for row in conn.execute("SELECT id, status FROM facts WHERE id LIKE 'fact_resolver_%'")
        }
    assert action["proposed_by"] == "resolver"
    assert statuses in (
        {"fact_resolver_left": "active", "fact_resolver_right": "superseded"},
        {"fact_resolver_left": "superseded", "fact_resolver_right": "active"},
    )


def test_resolver_llm_can_supersede_clear_newer_fact(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()
    insert_replacement_fact(
        paths,
        "fact_resolver_old",
        "The resolver test SLA is two days.",
        observed_at="2026-06-24T00:00:00+00:00",
        confidence=0.8,
    )
    insert_replacement_fact(
        paths,
        "fact_resolver_new",
        "The resolver test SLA is three days.",
        observed_at="2026-06-26T00:00:00+00:00",
        confidence=0.92,
    )

    result = resolve_fact_groups(
        paths,
        ["concepts:resolver-test:summary"],
        llm_provider=FakeResolverProvider("clear_supersession", keeper_fact_id="fact_resolver_new"),
    )

    assert result["auto_superseded"] == 1
    with connection(paths.sqlite_path) as conn:
        old = conn.execute("SELECT status FROM facts WHERE id = 'fact_resolver_old'").fetchone()
        new = conn.execute("SELECT status, supersedes_id FROM facts WHERE id = 'fact_resolver_new'").fetchone()
    assert old["status"] == "superseded"
    assert new["status"] == "active"
    assert new["supersedes_id"] == "fact_resolver_old"


def test_resolver_llm_same_claim_cannot_merge_direct_contradiction(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()
    insert_replacement_fact(
        paths,
        "fact_resolver_enabled",
        "AlphaPay auto-renewal is enabled by default for annual plans.",
    )
    insert_replacement_fact(
        paths,
        "fact_resolver_disabled",
        "AlphaPay auto-renewal is not enabled by default for annual plans.",
    )

    result = resolve_fact_groups(
        paths,
        ["concepts:resolver-test:summary"],
        llm_provider=FakeResolverProvider("same_claim"),
    )

    assert result["auto_merged"] == 0
    assert len(result["conflict_group_ids"]) == 1
    with connection(paths.sqlite_path) as conn:
        action = conn.execute("SELECT * FROM cos_actions WHERE action_type = 'display_contested'").fetchone()
        statuses = {
            row["id"]: row["status"]
            for row in conn.execute("SELECT id, status FROM facts WHERE id LIKE 'fact_resolver_%'")
        }
    assert action["proposed_by"] == "resolver"
    assert action["risk_tier"] == "high"
    assert statuses == {
        "fact_resolver_enabled": "conflicted",
        "fact_resolver_disabled": "conflicted",
    }


def test_answer_open_question_records_resolution_action(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()
    with connection(paths.sqlite_path) as conn:
        for fact_id, statement in [
            ("fact_answer_keep", "The answer flow should keep this fact."),
            ("fact_answer_supersede", "The answer flow should supersede this fact."),
        ]:
            conn.execute(
                """
                INSERT INTO facts(
                  id, statement, entity_key, page_hint, section_hint, source_ids,
                  observed_at, confidence, status, conflict_group_id, metadata,
                  created_at, last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    fact_id,
                    statement,
                    "concepts:answer-flow:summary",
                    "concepts/answer-flow.md",
                    "Summary",
                    json.dumps([f"document:{fact_id}"]),
                    "2026-06-23T00:00:00+00:00",
                    0.7,
                    "conflicted",
                    "factconflict_answer",
                    "{}",
                    "2026-06-23T00:00:00+00:00",
                    None,
                ),
            )
        conn.execute(
            """
            INSERT INTO open_questions(
              id, kind, entity_key, page_hint, fact_ids, question, options,
              status, context, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "question_answer_flow",
                "conflict",
                "concepts:answer-flow:summary",
                "concepts/answer-flow.md",
                json.dumps(["fact_answer_keep", "fact_answer_supersede"]),
                "Which answer-flow fact is current?",
                "[]",
                "open",
                json.dumps({"conflict_group_id": "factconflict_answer"}),
                "2026-06-23T00:00:00+00:00",
            ),
        )

    result = answer_open_question(
        paths,
        "question_answer_flow",
        selected_fact_id="fact_answer_keep",
        answer="Confirmed by test.",
    )

    assert result["actions"][0]["action_type"] == "resolve_conflict"
    assert result["actions"][0]["status"] == "applied"
    with connection(paths.sqlite_path) as conn:
        keep = conn.execute("SELECT status, confirmed_by_user FROM facts WHERE id = 'fact_answer_keep'").fetchone()
        superseded = conn.execute("SELECT status FROM facts WHERE id = 'fact_answer_supersede'").fetchone()
        question = conn.execute("SELECT status, action_id FROM open_questions WHERE id = 'question_answer_flow'").fetchone()

    assert keep["status"] == "active"
    assert keep["confirmed_by_user"] == 1
    assert superseded["status"] == "superseded"
    assert question["status"] == "answered"
    assert question["action_id"] == result["actions"][0]["id"]


class FakeResolverProvider:
    name = "fake-resolver"
    model = "fake-resolver-model"

    def __init__(self, decision: str, *, keeper_fact_id: str | None = None) -> None:
        self.decision = decision
        self.keeper_fact_id = keeper_fact_id

    def complete(self, prompt: str) -> str:
        assert "Resolve this group of source-backed PKM facts" in prompt
        return json.dumps(
            {
                "decision": self.decision,
                "fact_ids": [],
                "keeper_fact_id": self.keeper_fact_id,
                "rationale": "test resolver judgment",
                "risk_tier": "medium",
            }
        )


def insert_replacement_fact(
    paths: BrainPaths,
    fact_id: str,
    statement: str,
    *,
    observed_at: str = "2026-06-26T00:00:00+00:00",
    confidence: float = 0.9,
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
                "concepts:resolver-test:summary",
                "concepts/resolver-test.md",
                "Summary",
                json.dumps([f"document:{fact_id}"]),
                observed_at,
                confidence,
                "active",
                json.dumps({"operation": "replace_page"}),
                observed_at,
                confidence,
            ),
        )
