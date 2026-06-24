from __future__ import annotations

import http.client
import json
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from pkm_brain.db import connection, dumps
from pkm_brain.paths import BrainPaths
from pkm_brain.service import BrainService, memory_export_path
from pkm_brain.ui_server import create_ui_server, ensure_ui_token
from pkm_brain.wiki import GENERATED_MARKER
from pkm_brain.wiki_facts import facts_should_merge


@contextmanager
def running_ui(paths: BrainPaths) -> Iterator[tuple[str, int, str]]:
    token = ensure_ui_token(paths)
    server = create_ui_server(paths, "127.0.0.1", 0, token=token)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        yield str(host), int(port), token
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def request_json(
    host: str,
    port: int,
    token: str | None,
    method: str,
    path: str,
    body: dict[str, object] | None = None,
) -> tuple[int, dict[str, object]]:
    encoded = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    if encoded:
        headers["Content-Type"] = "application/json"
    conn = http.client.HTTPConnection(host, port, timeout=5)
    conn.request(method, path, body=encoded, headers=headers)
    response = conn.getresponse()
    response_body = response.read().decode("utf-8")
    conn.close()
    return response.status, json.loads(response_body)


def insert_document(paths: BrainPaths, document_id: str = "doc_source") -> None:
    paths.raw.mkdir(parents=True, exist_ok=True)
    raw_path = paths.raw / f"{document_id}.md"
    raw_path.write_text("Source evidence body.", encoding="utf-8")
    with connection(paths.sqlite_path) as conn:
        conn.execute(
            """
            INSERT INTO documents(
              id, source_type, title, source_path, raw_path, content_hash,
              origin_node_id, logical_source_key, created_at, ingested_at,
              project, tags, version, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                document_id,
                "markdown_note",
                "Source Evidence",
                str(raw_path),
                str(raw_path),
                "hash-source",
                "<local>",
                str(raw_path),
                "2026-05-25T00:00:00+00:00",
                "2026-05-25T00:00:00+00:00",
                None,
                "[]",
                1,
                "active",
            ),
        )


def write_concept_page(paths: BrainPaths, *, generated: bool = False) -> Path:
    target = paths.wiki / "concepts"
    target.mkdir(parents=True, exist_ok=True)
    marker = f"{GENERATED_MARKER}\n" if generated else ""
    page = target / "test-concept.md"
    page.write_text(
        "---\n"
        "title: Test Concept\n"
        "page_type: concept\n"
        "id: concept-test\n"
        "status: active\n"
        "created_at: 2026-05-24\n"
        "updated_at: 2026-05-25\n"
        "source_ids:\n"
        "  - document:doc_source\n"
        "  - document:missing\n"
        "  - manual:note\n"
        "related: []\n"
        "tags: []\n"
        "---\n\n"
        f"{marker}"
        "# Test Concept\n\n"
        "## Summary\n\nOld summary.\n\n"
        "## Key Points\n\n- Old point.\n\n"
        "## Definition\n\nOld definition.\n\n"
        "## Why It Matters\n\nOld rationale.\n\n"
        "## How It Works\n\nOld mechanics.\n\n"
        "## Related Decisions\n\n- None.\n\n"
        "## Source Evidence\n\n- document:doc_source\n\n"
        "## Related Pages\n\n- None.\n\n"
        "## Open Questions\n\n- None.\n",
        encoding="utf-8",
    )
    return page


def test_status_endpoint_returns_service_layer_json(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")

    with running_ui(paths) as (host, port, token):
        status, body = request_json(host, port, token, "GET", "/api/status")

    assert status == 200
    assert body["doctor"]["home"] == str(paths.home)
    assert "index" in body
    assert "memory" in body
    surfaces = {surface["surface"]: surface for surface in body["retrieval_surfaces"]}
    assert surfaces["Fact ledger"]["searched"] is True
    assert surfaces["Raw source chunks"]["searched"] is True
    assert surfaces["Wiki pages"]["searched"] is True
    assert surfaces["Memories"]["searched"] is True
    assert "Legacy wiki packets" not in surfaces
    assert surfaces["CoS action ledger"]["searched"] is False


def test_legacy_wiki_proposal_endpoints_are_retired(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths, prefer_model_embeddings=False).init_workspace()

    with running_ui(paths) as (host, port, token):
        checks = [
            request_json(host, port, token, "GET", "/api/wiki/proposals"),
            request_json(host, port, token, "GET", "/api/wiki/proposals/batch_old"),
            request_json(host, port, token, "GET", "/api/wiki/proposal-packets"),
            request_json(host, port, token, "GET", "/api/review-queue"),
            request_json(host, port, token, "POST", "/api/wiki/proposals", {}),
            request_json(host, port, token, "POST", "/api/wiki/proposal-packets/facts", {}),
        ]

    assert [status for status, _body in checks] == [404, 404, 404, 404, 404, 404]


def test_memory_endpoint_lists_status_filtered_memories(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    svc = BrainService(paths, prefer_model_embeddings=False)
    memory_id = svc.propose_memory(
        "FactMemory", "global", "Use the local UI for review.", ["manual:test"], 0.9
    )

    with running_ui(paths) as (host, port, token):
        status, body = request_json(
            host, port, token, "GET", "/api/memory?status=proposed"
        )

    assert status == 200
    assert body["count"] == 1
    assert body["memories"][0]["id"] == memory_id


def test_memory_approve_endpoint_writes_same_export_as_cli_path(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    svc = BrainService(paths, prefer_model_embeddings=False)
    memory_id = svc.propose_memory(
        "FactMemory", "global", "Approve from the UI.", ["manual:test"], 0.95
    )

    with running_ui(paths) as (host, port, token):
        status, body = request_json(
            host, port, token, "POST", f"/api/memory/{memory_id}/approve"
        )

    assert status == 200
    assert body["status"] == "active"
    assert memory_export_path(paths, svc.get_memory(memory_id)).exists()


def test_memory_detail_joins_document_source_evidence(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    svc = BrainService(paths, prefer_model_embeddings=False)
    svc.init_workspace()
    insert_document(paths)
    memory_id = svc.propose_memory(
        "FactMemory",
        "global",
        "Source-backed memory.",
        ["document:doc_source", "document:missing", "manual:test"],
        0.9,
    )

    with running_ui(paths) as (host, port, token):
        status, body = request_json(
            host, port, token, "GET", f"/api/memory/{memory_id}"
        )

    assert status == 200
    assert body["source_ids"] == [
        "document:doc_source",
        "document:missing",
        "manual:test",
    ]
    assert [document["source_id"] for document in body["source_documents"]] == [
        "document:doc_source"
    ]


def test_wiki_pages_endpoint_requires_auth_and_returns_indexed_pages(
    tmp_path: Path,
) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    svc = BrainService(paths, prefer_model_embeddings=False)
    svc.init_workspace()
    insert_document(paths)
    write_concept_page(paths, generated=True)

    with running_ui(paths) as (host, port, token):
        unauth_status, unauth_body = request_json(
            host, port, None, "GET", "/api/wiki/pages"
        )
        status, body = request_json(host, port, token, "GET", "/api/wiki/pages")
        search_status, search_body = request_json(
            host, port, token, "GET", "/api/wiki/pages?q=summary"
        )

    assert unauth_status == 401
    assert "token" in str(unauth_body["error"])
    assert status == 200
    assert body["count"] == 1
    page = body["pages"][0]
    assert page["relative_path"] == "concepts/test-concept.md"
    assert page["source_count"] == 3
    assert page["generated"] is True
    assert search_status == 200
    assert search_body["pages"][0]["relative_path"] == "concepts/test-concept.md"


def test_wiki_page_endpoint_validates_path_and_joins_sources(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    svc = BrainService(paths, prefer_model_embeddings=False)
    svc.init_workspace()
    insert_document(paths)
    write_concept_page(paths, generated=True)

    with running_ui(paths) as (host, port, token):
        bad_status, bad_body = request_json(
            host, port, token, "GET", "/api/wiki/page?path=../outside.md"
        )
        status, body = request_json(
            host, port, token, "GET", "/api/wiki/page?path=concepts/test-concept.md"
        )

    assert bad_status == 400
    assert "relative" in str(bad_body["error"])
    assert status == 200
    assert body["frontmatter"]["title"] == "Test Concept"
    assert "Old summary." in body["body"]
    assert body["generated"] is True
    assert body["source_ids"] == [
        "document:doc_source",
        "document:missing",
        "manual:note",
    ]
    assert [document["title"] for document in body["source_documents"]] == [
        "Source Evidence"
    ]


def test_cloudzero_source_backed_alternatives_merge_as_same_fact() -> None:
    left_sources = [
        "document:doc_3efaf238ca4649bf",
        "document:doc_0fb3ff53e697420e",
        "document:doc_8f6c7b88db164ff2",
        "document:doc_5bfd3423899c4e8d",
        "document:doc_74c726cceaa4468e",
        "document:doc_d574c38ab94546af",
    ]
    right_sources = [
        "document:doc_3efaf238ca4649bf",
        "document:doc_0fb3ff53e697420e",
        "document:doc_d574c38ab94546af",
    ]
    left = (
        "The CloudZero interview prompt centered on designing capabilities for a FinOps administrator to "
        "share dashboards and related analysis with immediate teammates and leaders across the organization. "
        "The core problem is not merely distributing dashboards. It is enabling the right people to collaborate "
        "on or consume financial and cost analysis based on their role, team context, and permissions. Primary "
        "users discussed: FinOps administrator, internal FinOps teammates, and cross-functional team leads who "
        "need visibility into cost or financial reporting. A useful access model distinguishes between groups "
        "inherited from an identity source and custom groups created by a FinOps administrator."
    )
    right = (
        "The May 29, 2026 CloudZero interview focused on designing capabilities for a FinOps administrator to "
        "share dashboards and related analysis with both their immediate team and leaders in other parts of the "
        "organization. The core problem was not simply publishing dashboards broadly. The stronger framing was "
        "role-aware collaboration and consumption: FinOps admins need to decide who can view, edit, or manage "
        "shared analysis; internal FinOps teammates may collaborate directly; cross-functional leaders may need "
        "curated visibility into cost or financial reporting without full administrative control."
    )

    assert (
        facts_should_merge(
            {"statement": left, "source_ids": left_sources},
            {"statement": right, "source_ids": right_sources},
        )
        is True
    )


def test_wiki_fact_reconcile_dismisses_stale_duplicate_cloudzero_question(
    tmp_path: Path,
) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    svc = BrainService(paths, prefer_model_embeddings=False)
    svc.init_workspace()
    insert_document(paths)
    write_concept_page(paths, generated=True)
    left_sources = [
        "document:doc_3efaf238ca4649bf",
        "document:doc_0fb3ff53e697420e",
        "document:doc_8f6c7b88db164ff2",
        "document:doc_5bfd3423899c4e8d",
        "document:doc_74c726cceaa4468e",
        "document:doc_d574c38ab94546af",
    ]
    right_sources = [
        "document:doc_3efaf238ca4649bf",
        "document:doc_0fb3ff53e697420e",
        "document:doc_d574c38ab94546af",
    ]
    left = (
        "The CloudZero interview prompt centered on designing capabilities for a FinOps administrator to "
        "share dashboards and related analysis with immediate teammates and leaders across the organization. "
        "The core problem is not merely distributing dashboards. It is enabling role-aware collaboration and "
        "consumption of financial and cost analysis based on team context and permissions."
    )
    right = (
        "The May 29, 2026 CloudZero interview focused on designing capabilities for a FinOps administrator to "
        "share dashboards and related analysis with both their immediate team and leaders in other parts of the "
        "organization. The stronger framing was role-aware collaboration and consumption of cost analysis."
    )
    fact_ids = ["fact_cloudzero_a", "fact_cloudzero_b"]
    question_id = "question_cloudzero"
    with connection(paths.sqlite_path) as conn:
        for fact_id, statement, source_ids, observed_at in [
            (fact_ids[0], left, left_sources, "2026-05-30T08:03:04+00:00"),
            (fact_ids[1], right, right_sources, "2026-05-30T09:03:04+00:00"),
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
                    "concepts:cloudzero-dashboard-sharing-interview:summary",
                    "concepts/test-concept.md",
                    "Summary",
                    dumps(source_ids),
                    observed_at,
                    0.84,
                    "conflicted",
                    "factconflict_cloudzero",
                    dumps(
                        {
                            "operation": "replace_section",
                            "target_path": "concepts/test-concept.md",
                        }
                    ),
                    observed_at,
                    observed_at,
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
                question_id,
                "conflict",
                "concepts:cloudzero-dashboard-sharing-interview:summary",
                "concepts/test-concept.md",
                dumps(fact_ids),
                "What is currently true for CloudZero dashboard sharing?",
                dumps(
                    [
                        {
                            "fact_id": fact_ids[0],
                            "statement": left,
                            "source_ids": left_sources,
                        },
                        {
                            "fact_id": fact_ids[1],
                            "statement": right,
                            "source_ids": right_sources,
                        },
                    ]
                ),
                "open",
                dumps({"conflict_group_id": "factconflict_cloudzero"}),
                "2026-05-30T10:03:04+00:00",
            ),
        )

    with running_ui(paths) as (host, port, token):
        status, body = request_json(
            host, port, token, "POST", "/api/wiki/facts/reconcile"
        )

    assert status == 200
    assert body["merged_facts"] == 1
    assert body["dismissed_question_ids"] == [question_id]
    assert body["updated_question_ids"] == []
    assert body["dashboard"]["open_questions"] == []
    assert body["dashboard"]["counts"]["by_status"]["active"] == 1
    assert body["dashboard"]["counts"]["by_status"]["superseded"] == 1
    assert body["dashboard"]["counts"]["questions_by_status"]["dismissed"] == 1


def test_wiki_fact_migration_backfills_existing_wiki(
    tmp_path: Path,
) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    svc = BrainService(paths, prefer_model_embeddings=False)
    svc.init_workspace()
    insert_document(paths)
    page = paths.wiki / "concepts" / "test-concept.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(
        "---\n"
        "title: Test Concept\n"
        "page_type: concept\n"
        "id: concept-test\n"
        "status: active\n"
        "created_at: 2026-05-24\n"
        "updated_at: 2026-05-25\n"
        "source_ids:\n"
        "  - document:doc_source\n"
        "related: []\n"
        "tags: []\n"
        "---\n\n"
        "# Test Concept\n\n"
        "## Summary\n\nThe Sierra final interview is scheduled for Monday.\n\n"
        "## Key Points\n\n- CloudZero dashboard sharing needs role-aware collaboration.\n\n"
        "## Definition\n\nNone.\n\n"
        "## Why It Matters\n\nNone.\n\n"
        "## How It Works\n\nNone.\n\n"
        "## Related Decisions\n\n- None.\n\n"
        "## Source Evidence\n\n- document:doc_source\n\n"
        "## Related Pages\n\n- None.\n\n"
        "## Open Questions\n\n- None.\n",
        encoding="utf-8",
    )

    with running_ui(paths) as (host, port, token):
        preview_status, preview = request_json(
            host,
            port,
            token,
            "POST",
            "/api/wiki/facts/migrate-wiki",
            {"dry_run": True},
        )
        apply_status, applied = request_json(
            host,
            port,
            token,
            "POST",
            "/api/wiki/facts/migrate-wiki",
            {"dry_run": False},
        )
        rerun_status, rerun = request_json(
            host,
            port,
            token,
            "POST",
            "/api/wiki/facts/migrate-wiki",
            {"dry_run": False},
        )

    assert preview_status == 200
    assert preview["dry_run"] is True
    assert preview["candidate_count"] == 2
    assert preview["new_candidate_count"] == 2
    assert apply_status == 200
    assert applied["dry_run"] is False
    assert len(applied["created_fact_ids"]) == 2
    assert applied["dashboard"]["counts"]["by_status"]["active"] == 2
    assert applied["curation"]["pages"][0]["written"] is False
    assert "not managed" in applied["curation"]["pages"][0]["reason"]
    assert rerun_status == 200
    assert rerun["new_candidate_count"] == 0
    assert rerun["created_fact_ids"] == []


def test_chief_of_staff_page_review_correction_and_revert_endpoint(
    tmp_path: Path,
) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    svc = BrainService(paths, prefer_model_embeddings=False)
    svc.init_workspace()
    insert_document(paths)
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
                "fact_ui_original",
                "The Chief of Staff endpoint starts with the original fact.",
                "concepts:test-concept:summary",
                "concepts/test-concept.md",
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

    with running_ui(paths) as (host, port, token):
        preview_status, preview = request_json(
            host,
            port,
            token,
            "POST",
            "/api/wiki/facts/pages/regenerate",
            {"page_hint": "concepts/test-concept.md", "dry_run": True},
        )
        apply_status, applied = request_json(
            host,
            port,
            token,
            "POST",
            "/api/wiki/facts/pages/regenerate",
            {"page_hint": "concepts/test-concept.md", "dry_run": False},
        )
        review_status, review = request_json(
            host,
            port,
            token,
            "GET",
            "/api/wiki/facts/page?path=concepts/test-concept.md",
        )
        correction_status, correction = request_json(
            host,
            port,
            token,
            "POST",
            "/api/wiki/facts/corrections",
            {
                "page_hint": "concepts/test-concept.md",
                "statement": "The Chief of Staff endpoint now uses the corrected fact.",
                "supersede_fact_ids": ["fact_ui_original"],
            },
        )
        snapshot_id = correction["curation"]["pages"][0]["snapshot_id"]
        revert_status, reverted = request_json(
            host,
            port,
            token,
            "POST",
            "/api/wiki/facts/pages/revert",
            {"snapshot_id": snapshot_id},
        )

    assert preview_status == 200
    assert "original fact" in preview["review"]["draft_markdown"]
    assert apply_status == 200
    assert applied["curation"]["pages"][0]["snapshot_id"].startswith("wikisnap_")
    assert review_status == 200
    assert review["snapshots"]
    assert correction_status == 200
    assert correction["fact"]["confirmed_by_user"] is True
    assert "corrected fact" in correction["review"]["current_markdown"]
    assert revert_status == 200
    assert "original fact" in reverted["review"]["current_markdown"]


def test_cos_control_plane_endpoints_require_auth_and_return_state(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()
    with running_ui(paths) as (host, port, token):
        unauthorized, _ = request_json(host, port, None, "GET", "/api/cos/policy")
        policy_status, policy = request_json(host, port, token, "GET", "/api/cos/policy")
        actions_status, actions = request_json(host, port, token, "GET", "/api/cos/actions")
        contracts_status, contracts = request_json(host, port, token, "GET", "/api/cos/contracts")
        audit_status, audit = request_json(host, port, token, "GET", "/api/cos/audit")

    assert unauthorized == 401
    assert policy_status == 200
    assert policy["version"] == 1
    assert policy["rules"]
    assert actions_status == 200
    assert actions["actions"] == []
    assert contracts_status == 200
    assert contracts["contracts"] == []
    assert audit_status == 200
    assert audit["counts"] == {}
