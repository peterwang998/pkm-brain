from __future__ import annotations

import json
from pathlib import Path

from pkm_brain.db import connection
from pkm_brain.paths import BrainPaths
from pkm_brain.service import BrainService
from pkm_brain.wiki_curation_promote import promote_wiki_curation
from pkm_brain.wiki_fact_migration import extract_fact_statements
from pkm_brain.wiki_facts import (
    archive_orphan_managed_pages,
    compact_statement,
    create_confirmed_page_fact,
    duplicate_fact_projection_errors,
    managed_fact_page_review,
    render_managed_page,
    regenerate_managed_fact_page,
    revert_wiki_page_snapshot,
)


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
    corrected_markdown = page.read_text(encoding="utf-8")
    assert "corrected fact" in corrected_markdown
    assert "original fact" not in corrected_markdown

    review = managed_fact_page_review(paths, "concepts/managed-review.md")
    correction_snapshot_id = corrected["curation"]["pages"][0]["snapshot_id"]
    assert correction_snapshot_id in {snapshot["id"] for snapshot in review["snapshots"]}
    reverted = revert_wiki_page_snapshot(paths, correction_snapshot_id)

    assert reverted["revert_snapshot_id"].startswith("wikisnap_")
    reverted_markdown = page.read_text(encoding="utf-8")
    assert "original fact" in reverted_markdown
    assert "corrected fact" not in reverted_markdown
