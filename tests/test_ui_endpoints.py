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
from pkm_brain.wiki_facts import CHIEF_OF_STAFF_MARKER, facts_should_merge
from pkm_brain.wiki_proposals import (
    build_wiki_review_packet_context,
    create_wiki_proposal,
    inspect_wiki_proposal,
)


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


def create_section_proposal(paths: BrainPaths) -> str:
    return create_wiki_proposal(
        paths,
        title="Update test concept",
        rationale="Better summary.",
        source_ids=["document:doc_source", "manual:note"],
        changes=[
            {
                "target_path": "concepts/test-concept.md",
                "operation": "replace_section",
                "section_name": "Summary",
                "proposed_markdown": "New source-backed summary.",
                "rationale": "Improve summary.",
                "source_ids": ["document:doc_source"],
                "confidence": 0.95,
            }
        ],
        confidence=0.95,
    )


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
    assert surfaces["Legacy wiki packets"]["searched"] is False
    assert surfaces["CoS action ledger"]["searched"] is False


def test_status_endpoint_counts_only_reviewable_legacy_packets(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    svc = BrainService(paths, prefer_model_embeddings=False)
    svc.init_workspace()
    insert_document(paths)
    write_concept_page(paths)
    create_section_proposal(paths)
    rejected_batch = create_section_proposal(paths)
    with connection(paths.sqlite_path) as conn:
        conn.execute(
            "UPDATE wiki_change_batches SET status = 'rejected' WHERE id = ?",
            (rejected_batch,),
        )

    with running_ui(paths) as (host, port, token):
        status, body = request_json(host, port, token, "GET", "/api/status")

    surfaces = {surface["surface"]: surface for surface in body["retrieval_surfaces"]}
    assert status == 200
    assert surfaces["Legacy wiki packets"]["count"] == 1
    assert surfaces["Legacy wiki packets"]["searched"] is False


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


def test_wiki_proposal_detail_previews_replace_section_and_approve_apply(
    tmp_path: Path,
) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    svc = BrainService(paths, prefer_model_embeddings=False)
    svc.init_workspace()
    insert_document(paths)
    page = write_concept_page(paths)
    batch_id = create_section_proposal(paths)

    with running_ui(paths) as (host, port, token):
        list_status, list_body = request_json(
            host, port, token, "GET", "/api/wiki/proposals?status=proposed"
        )
        detail_status, detail_body = request_json(
            host, port, token, "GET", f"/api/wiki/proposals/{batch_id}"
        )
        expected_markdown = detail_body["items"][0]["would_be_markdown"]
        apply_status, apply_body = request_json(
            host,
            port,
            token,
            "POST",
            f"/api/wiki/proposals/{batch_id}/approve-and-apply",
            {"questions": ["Approve?"], "answers": ["Yes."]},
        )
        queue_status, queue_body = request_json(
            host, port, token, "GET", "/api/review-queue"
        )

    assert list_status == 200
    assert list_body["count"] == 1
    assert detail_status == 200
    item = detail_body["items"][0]
    assert item["current_markdown"] == "Old summary."
    assert "New source-backed summary." in item["would_be_markdown"]
    assert item["source_documents"][0]["source_id"] == "document:doc_source"
    assert apply_status == 200
    assert apply_body["lint"]["errors"] == []
    assert apply_body["proposal"]["status"] == "applied"
    assert queue_status == 200
    assert batch_id not in [item["id"] for item in queue_body["items"]]
    assert page.read_text(encoding="utf-8") == expected_markdown


def test_wiki_proposal_apply_lint_failure_restores_original_file(
    tmp_path: Path,
) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    svc = BrainService(paths, prefer_model_embeddings=False)
    svc.init_workspace()
    insert_document(paths)
    page = write_concept_page(paths)
    original = page.read_text(encoding="utf-8")
    batch_id = create_wiki_proposal(
        paths,
        title="Break page",
        rationale="Exercise rollback.",
        source_ids=["document:doc_source"],
        changes=[
            {
                "target_path": "concepts/test-concept.md",
                "operation": "replace_page",
                "proposed_markdown": "# Invalid page\n",
                "rationale": "Invalid markdown.",
                "source_ids": ["document:doc_source"],
                "confidence": 0.2,
            }
        ],
        confidence=0.2,
    )

    with running_ui(paths) as (host, port, token):
        approve_status, _approve_body = request_json(
            host,
            port,
            token,
            "POST",
            f"/api/wiki/proposals/{batch_id}/interview",
            {"questions": ["Approve?"], "answers": ["No."], "disposition": "approved"},
        )
        apply_status, apply_body = request_json(
            host, port, token, "POST", f"/api/wiki/proposals/{batch_id}/apply"
        )

    assert approve_status == 200
    assert apply_status == 200
    assert apply_body["lint"]["errors"]
    assert apply_body["proposal"]["status"] == "failed"
    assert page.read_text(encoding="utf-8") == original


def test_wiki_proposal_reject_and_generate_questions_endpoints(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    svc = BrainService(paths, prefer_model_embeddings=False)
    svc.init_workspace()
    insert_document(paths)
    write_concept_page(paths)
    batch_id = create_section_proposal(paths)

    with running_ui(paths) as (host, port, token):
        question_status, question_body = request_json(
            host,
            port,
            token,
            "POST",
            f"/api/wiki/proposals/{batch_id}/interview/generate",
            {"provider": "missing-provider"},
        )
        empty_reject_status, _empty_reject_body = request_json(
            host,
            port,
            token,
            "POST",
            f"/api/wiki/proposals/{batch_id}/reject",
            {"reason": ""},
        )
        reject_status, reject_body = request_json(
            host,
            port,
            token,
            "POST",
            f"/api/wiki/proposals/{batch_id}/reject",
            {"reason": "Not accurate enough."},
        )

    assert question_status == 200
    assert len(question_body["questions"]) == 3
    assert empty_reject_status == 400
    assert reject_status == 200
    assert reject_body["status"] == "rejected"
    assert reject_body["error"] == "Not accurate enough."


def test_wiki_question_generation_falls_back_on_provider_runtime_failure(
    tmp_path: Path, monkeypatch
) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    svc = BrainService(paths, prefer_model_embeddings=False)
    svc.init_workspace()
    insert_document(paths)
    write_concept_page(paths)
    batch_id = create_section_proposal(paths)

    class FailingProvider:
        name = "codex"
        model = "broken-model"

        def complete(self, _prompt: str) -> str:
            raise RuntimeError("provider unavailable")

    monkeypatch.setattr(
        "pkm_brain.wiki_proposals.get_provider",
        lambda _provider_name: FailingProvider(),
    )

    with running_ui(paths) as (host, port, token):
        status, body = request_json(
            host,
            port,
            token,
            "POST",
            f"/api/wiki/proposals/{batch_id}/interview/generate",
            {"provider": "codex"},
        )

    assert status == 200
    assert len(body["questions"]) == 3
    assert body["provider"] is None
    assert body["model"] is None


def test_review_queue_aggregates_pending_memories_and_wiki_proposals(
    tmp_path: Path,
) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    svc = BrainService(paths, prefer_model_embeddings=False)
    svc.init_workspace()
    insert_document(paths)
    write_concept_page(paths)
    memory_id = svc.propose_memory(
        "FactMemory", "global", "Review queue memory.", ["document:doc_source"], 0.8
    )
    batch_id = create_section_proposal(paths)
    active_memory = svc.propose_memory(
        "FactMemory", "global", "Already active.", ["manual:test"], 0.8
    )
    svc.approve_memory(active_memory)
    rejected_batch = create_section_proposal(paths)
    with connection(paths.sqlite_path) as conn:
        conn.execute(
            "UPDATE wiki_change_batches SET status = 'rejected' WHERE id = ?",
            (rejected_batch,),
        )
        conn.execute(
            "UPDATE memories SET created_at = ? WHERE id = ?",
            ("2026-05-25T00:00:00+00:00", memory_id),
        )
        conn.execute(
            "UPDATE wiki_change_batches SET created_at = ? WHERE id = ?",
            ("2026-05-26T00:00:00+00:00", batch_id),
        )

    with running_ui(paths) as (host, port, token):
        status, body = request_json(host, port, token, "GET", "/api/review-queue")

    assert status == 200
    assert body["count"] == 2
    assert [(item["kind"], item["id"]) for item in body["items"]] == [
        ("wiki_proposal", batch_id),
        ("memory", memory_id),
    ]


def test_wiki_proposal_packets_group_backlog_by_topic_and_page(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    svc = BrainService(paths, prefer_model_embeddings=False)
    svc.init_workspace()
    insert_document(paths)
    write_concept_page(paths)
    older_batch = create_section_proposal(paths)
    newer_batch = create_wiki_proposal(
        paths,
        title="Revise test concept again",
        rationale="A newer candidate for the same section.",
        source_ids=["document:doc_source"],
        changes=[
            {
                "target_path": "concepts/test-concept.md",
                "operation": "replace_section",
                "section_name": "Summary",
                "proposed_markdown": "Newest source-backed summary.",
                "rationale": "A later replacement candidate.",
                "source_ids": ["document:doc_source"],
                "confidence": 0.97,
            }
        ],
        confidence=0.97,
        status="needs_interview",
    )
    new_page_batch = create_wiki_proposal(
        paths,
        title="Create project page",
        rationale="New project page candidate.",
        source_ids=["document:doc_source"],
        changes=[
            {
                "target_path": "projects/new-project.md",
                "operation": "create_page",
                "proposed_markdown": "---\ntitle: New Project\npage_type: project\nid: project-new\nstatus: draft\nsource_ids:\n  - document:doc_source\nrelated: []\ntags: []\n---\n\n# New Project\n",
                "rationale": "Create a project page.",
                "source_ids": ["document:doc_source"],
                "confidence": 0.8,
            }
        ],
        confidence=0.8,
    )
    rejected_batch = create_section_proposal(paths)
    with connection(paths.sqlite_path) as conn:
        conn.execute(
            "UPDATE wiki_change_batches SET created_at = ? WHERE id = ?",
            ("2026-05-24T00:00:00+00:00", older_batch),
        )
        conn.execute(
            "UPDATE wiki_change_batches SET created_at = ? WHERE id = ?",
            ("2026-05-25T00:00:00+00:00", newer_batch),
        )
        conn.execute(
            "UPDATE wiki_change_batches SET created_at = ? WHERE id = ?",
            ("2026-05-26T00:00:00+00:00", new_page_batch),
        )
        conn.execute(
            "UPDATE wiki_change_batches SET status = 'rejected' WHERE id = ?",
            (rejected_batch,),
        )

    with running_ui(paths) as (host, port, token):
        status, body = request_json(
            host, port, token, "GET", "/api/wiki/proposal-packets?group_by=topic"
        )
        day_status, day_body = request_json(
            host, port, token, "GET", "/api/wiki/proposal-packets?group_by=day"
        )
        bad_status, bad_body = request_json(
            host, port, token, "GET", "/api/wiki/proposal-packets?group_by=invalid"
        )

    assert status == 200
    assert body["totals"]["packet_count"] == 2
    assert body["totals"]["target_count"] == 2
    assert body["totals"]["batch_count"] == 3
    assert body["totals"]["item_count"] == 3
    packets_by_id = {packet["id"]: packet for packet in body["packets"]}
    assert set(packets_by_id) == {"topic:concepts", "topic:projects"}
    concept_page = packets_by_id["topic:concepts"]["pages"][0]
    assert concept_page["target_path"] == "concepts/test-concept.md"
    assert concept_page["complexity"] == "stacked"
    assert concept_page["latest_batch_id"] == newer_batch
    assert concept_page["operation_groups"][0]["old_revision_count"] == 1
    assert "latest replacement" in concept_page["resolution_hint"]
    assert len(concept_page["proposals"]) == 2
    project_page = packets_by_id["topic:projects"]["pages"][0]
    assert project_page["target_path"] == "projects/new-project.md"
    assert project_page["complexity"] == "simple"
    assert project_page["target_exists"] is False
    assert day_status == 200
    assert day_body["totals"]["packet_count"] == 2
    assert bad_status == 400
    assert "group_by" in str(bad_body["error"])


def test_wiki_packet_brief_uses_provider_for_topic_specific_aggregation(
    tmp_path: Path, monkeypatch
) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    svc = BrainService(paths, prefer_model_embeddings=False)
    svc.init_workspace()
    insert_document(paths)
    write_concept_page(paths)
    older_batch = create_section_proposal(paths)
    newer_batch = create_wiki_proposal(
        paths,
        title="Revise test concept again",
        rationale="A newer candidate for the same section.",
        source_ids=["document:doc_source"],
        changes=[
            {
                "target_path": "concepts/test-concept.md",
                "operation": "replace_section",
                "section_name": "Summary",
                "proposed_markdown": "Newest source-backed summary.",
                "rationale": "A later replacement candidate.",
                "source_ids": ["document:doc_source"],
                "confidence": 0.97,
            }
        ],
        confidence=0.97,
        status="needs_interview",
    )
    with connection(paths.sqlite_path) as conn:
        conn.execute(
            "UPDATE wiki_change_batches SET created_at = ? WHERE id = ?",
            ("2026-05-24T00:00:00+00:00", older_batch),
        )
        conn.execute(
            "UPDATE wiki_change_batches SET created_at = ? WHERE id = ?",
            ("2026-05-25T00:00:00+00:00", newer_batch),
        )

    prompts: list[str] = []

    class PacketBriefProvider:
        name = "codex"
        model = "fake-model"

        def complete(self, prompt: str) -> str:
            prompts.append(prompt)
            return json.dumps(
                {
                    "summary": ["Concepts packet has one stacked page."],
                    "aggregation_strategy": "Use newest high-confidence source-backed replacement as draft candidate.",
                    "priority_targets": [
                        {
                            "target_path": "concepts/test-concept.md",
                            "priority": "stacked",
                            "reason": "Two replacements for Summary.",
                            "recommended_action": "draft",
                        }
                    ],
                    "conflicts": [],
                    "questions": [
                        {
                            "target_path": "concepts/test-concept.md",
                            "question": "Does the newer summary preserve the missing nuance?",
                            "why": "Latest candidate is high-confidence but still a replacement.",
                            "blocking": True,
                        }
                    ],
                    "consolidated_drafts": [
                        {
                            "target_path": "concepts/test-concept.md",
                            "operation": "replace_section",
                            "section_name": "Summary",
                            "proposed_markdown": "Newest source-backed summary.",
                            "rationale": "Use the newest high-confidence source-backed replacement.",
                            "source_ids": ["document:doc_source"],
                            "source_batch_ids": [newer_batch],
                            "confidence": 0.97,
                            "review_notes": "Review before creating a proposal.",
                        }
                    ],
                    "defer_or_reject": [],
                }
            )

    monkeypatch.setattr(
        "pkm_brain.wiki_proposals.get_provider",
        lambda _provider_name: PacketBriefProvider(),
    )

    with running_ui(paths) as (host, port, token):
        status, body = request_json(
            host,
            port,
            token,
            "POST",
            "/api/wiki/proposal-packets/brief",
            {"group_by": "topic", "packet_id": "topic:concepts", "provider": "codex"},
        )

    assert status == 200
    assert body["provider"] == "codex"
    assert body["model"] == "fake-model"
    assert body["packet"]["id"] == "topic:concepts"
    assert (
        body["questions"][0]["question"]
        == "Does the newer summary preserve the missing nuance?"
    )
    assert body["consolidated_drafts"][0]["target_path"] == "concepts/test-concept.md"
    assert (
        body["consolidated_drafts"][0]["proposed_markdown"]
        == "Newest source-backed summary."
    )
    assert prompts
    assert "prefer the more recent entry" in prompts[0]
    assert "Newest source-backed summary." in prompts[0]


def test_wiki_packet_fact_absorption_writes_missing_managed_page(
    tmp_path: Path,
) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    svc = BrainService(paths, prefer_model_embeddings=False)
    svc.init_workspace()
    insert_document(paths)
    batch_id = create_wiki_proposal(
        paths,
        title="Create managed fact page",
        rationale="Source-backed new concept.",
        source_ids=["document:doc_source"],
        changes=[
            {
                "target_path": "concepts/new-managed-fact.md",
                "operation": "create_page",
                "proposed_markdown": (
                    "---\n"
                    "title: New Managed Fact\n"
                    "page_type: concept\n"
                    "id: concept-new-managed-fact\n"
                    "status: active\n"
                    "source_ids:\n"
                    "  - document:doc_source\n"
                    "related: []\n"
                    "tags: []\n"
                    "---\n\n"
                    "# New Managed Fact\n\n"
                    "## Summary\n\nThis is a source-backed managed fact.\n"
                ),
                "rationale": "Create the page.",
                "source_ids": ["document:doc_source"],
                "confidence": 0.95,
            }
        ],
        confidence=0.95,
    )

    with running_ui(paths) as (host, port, token):
        status, body = request_json(
            host,
            port,
            token,
            "POST",
            "/api/wiki/proposal-packets/facts",
            {"group_by": "topic", "packet_id": "topic:concepts"},
        )
        dashboard_status, dashboard = request_json(
            host, port, token, "GET", "/api/wiki/facts"
        )
        queue_status, queue = request_json(
            host, port, token, "GET", "/api/review-queue"
        )

    page = paths.wiki / "concepts" / "new-managed-fact.md"
    proposal = inspect_wiki_proposal(paths, batch_id)
    assert status == 200
    assert body["candidate_count"] == 1
    assert len(body["created_fact_ids"]) == 1
    assert body["proposal_absorption"]["requested_item_count"] == 1
    assert body["proposal_absorption"]["fully_absorbed_batch_ids"] == [batch_id]
    assert proposal["status"] == "absorbed_by_facts"
    assert proposal["items"][0]["status"] == "absorbed_by_facts"
    assert queue_status == 200
    assert all(
        item["id"] != batch_id
        for item in queue["items"]
        if item["kind"] == "wiki_proposal"
    )
    assert body["curation"]["pages"][0]["written"] is True
    assert page.exists()
    assert CHIEF_OF_STAFF_MARKER in page.read_text(encoding="utf-8")
    assert dashboard_status == 200
    assert dashboard["counts"]["by_status"]["active"] == 1


def test_wiki_packet_fact_absorption_merges_similar_alternatives_without_question(
    tmp_path: Path,
) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    svc = BrainService(paths, prefer_model_embeddings=False)
    svc.init_workspace()
    insert_document(paths)
    write_concept_page(paths)
    first_batch = create_wiki_proposal(
        paths,
        title="Business idea wording A",
        rationale="First wording.",
        source_ids=["document:doc_source"],
        changes=[
            {
                "target_path": "concepts/test-concept.md",
                "operation": "replace_section",
                "section_name": "Summary",
                "proposed_markdown": "Peter is considering a business idea to recreate children's songs in Chinese with AI and publish them in English.",
                "rationale": "First wording.",
                "source_ids": ["document:doc_source"],
                "confidence": 0.68,
            }
        ],
        confidence=0.68,
    )
    second_batch = create_wiki_proposal(
        paths,
        title="Business idea wording B",
        rationale="Second wording.",
        source_ids=["document:doc_source"],
        changes=[
            {
                "target_path": "concepts/test-concept.md",
                "operation": "replace_section",
                "section_name": "Summary",
                "proposed_markdown": "Business idea: use AI to recreate children's songs in Chinese and publish the songs in English.",
                "rationale": "Second wording.",
                "source_ids": ["document:doc_source"],
                "confidence": 0.7,
            }
        ],
        confidence=0.7,
    )
    with connection(paths.sqlite_path) as conn:
        conn.execute(
            "UPDATE wiki_change_batches SET created_at = ? WHERE id = ?",
            ("2026-05-24T00:00:00+00:00", first_batch),
        )
        conn.execute(
            "UPDATE wiki_change_batches SET created_at = ? WHERE id = ?",
            ("2026-05-25T00:00:00+00:00", second_batch),
        )

    with running_ui(paths) as (host, port, token):
        status, body = request_json(
            host,
            port,
            token,
            "POST",
            "/api/wiki/proposal-packets/facts",
            {"group_by": "topic", "packet_id": "topic:concepts"},
        )
        dashboard_status, dashboard = request_json(
            host, port, token, "GET", "/api/wiki/facts"
        )

    assert status == 200
    assert len(body["created_fact_ids"]) == 1
    assert len(body["updated_fact_ids"]) == 1
    assert body["auto_merged"] == 1
    assert set(body["proposal_absorption"]["fully_absorbed_batch_ids"]) == {
        first_batch,
        second_batch,
    }
    assert body["resolved"]["created_question_ids"] == []
    assert body["dashboard"]["counts"]["questions"] == 0
    assert dashboard_status == 200
    assert dashboard["counts"]["by_status"]["active"] == 1
    assert dashboard["open_questions"] == []
    assert "Chinese" in dashboard["recent_facts"][0]["statement"]


def test_wiki_packet_fact_absorption_only_hides_consumed_items_in_mixed_batch(
    tmp_path: Path,
) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    svc = BrainService(paths, prefer_model_embeddings=False)
    svc.init_workspace()
    insert_document(paths)
    write_concept_page(paths)
    batch_id = create_wiki_proposal(
        paths,
        title="Mixed topic proposal",
        rationale="One batch touches concepts and projects.",
        source_ids=["document:doc_source"],
        changes=[
            {
                "target_path": "concepts/test-concept.md",
                "operation": "replace_section",
                "section_name": "Summary",
                "proposed_markdown": "Chief-of-staff curation should retire absorbed proposal items.",
                "rationale": "Update concept summary.",
                "source_ids": ["document:doc_source"],
                "confidence": 0.9,
            },
            {
                "target_path": "projects/other-project.md",
                "operation": "create_page",
                "proposed_markdown": (
                    "---\n"
                    "title: Other Project\n"
                    "page_type: project\n"
                    "id: project-other\n"
                    "status: draft\n"
                    "source_ids:\n"
                    "  - document:doc_source\n"
                    "related: []\n"
                    "tags: []\n"
                    "---\n\n"
                    "# Other Project\n\n"
                    "## Summary\n\nThis project remains pending.\n"
                ),
                "rationale": "Create project page.",
                "source_ids": ["document:doc_source"],
                "confidence": 0.82,
            },
        ],
        confidence=0.9,
    )

    with running_ui(paths) as (host, port, token):
        status, body = request_json(
            host,
            port,
            token,
            "POST",
            "/api/wiki/proposal-packets/facts",
            {"group_by": "topic", "packet_id": "topic:concepts"},
        )
        packet_status, packets = request_json(
            host, port, token, "GET", "/api/wiki/proposal-packets?group_by=topic"
        )
        queue_status, queue = request_json(
            host, port, token, "GET", "/api/review-queue"
        )

    proposal = inspect_wiki_proposal(paths, batch_id)
    statuses_by_target = {
        item["target_path"]: item["status"] for item in proposal["items"]
    }
    packets_by_id = {packet["id"]: packet for packet in packets["packets"]}

    assert status == 200
    assert body["proposal_absorption"]["fully_absorbed_batch_ids"] == []
    assert body["proposal_absorption"]["partially_absorbed_batch_ids"] == [batch_id]
    assert proposal["status"] == "proposed"
    assert statuses_by_target == {
        "concepts/test-concept.md": "absorbed_by_facts",
        "projects/other-project.md": "pending",
    }
    assert packet_status == 200
    assert set(packets_by_id) == {"topic:projects"}
    assert packets_by_id["topic:projects"]["item_count"] == 1
    assert queue_status == 200
    queue_item = next(
        item
        for item in queue["items"]
        if item["kind"] == "wiki_proposal" and item["id"] == batch_id
    )
    assert queue_item["pending_item_count"] == 1


def test_wiki_packet_fact_absorption_processes_all_pages_when_prompt_context_is_capped(
    tmp_path: Path,
) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    svc = BrainService(paths, prefer_model_embeddings=False)
    svc.init_workspace()
    insert_document(paths)
    for name in ["first", "second"]:
        create_wiki_proposal(
            paths,
            title=f"Create {name} managed concept",
            rationale=f"Create the {name} page.",
            source_ids=["document:doc_source"],
            changes=[
                {
                    "target_path": f"concepts/{name}-managed.md",
                    "operation": "create_page",
                    "proposed_markdown": (
                        "---\n"
                        f"title: {name.title()} Managed\n"
                        "page_type: concept\n"
                        f"id: concept-{name}-managed\n"
                        "status: active\n"
                        "source_ids:\n"
                        "  - document:doc_source\n"
                        "related: []\n"
                        "tags: []\n"
                        "---\n\n"
                        f"# {name.title()} Managed\n\n"
                        f"## Summary\n\nThe {name} managed page should be absorbed into facts.\n"
                    ),
                    "rationale": f"Create the {name} page.",
                    "source_ids": ["document:doc_source"],
                    "confidence": 0.9,
                }
            ],
            confidence=0.9,
        )
    capped_context = build_wiki_review_packet_context(
        paths, "topic:concepts", max_pages=1
    )

    with running_ui(paths) as (host, port, token):
        status, body = request_json(
            host,
            port,
            token,
            "POST",
            "/api/wiki/proposal-packets/facts",
            {"group_by": "topic", "packet_id": "topic:concepts"},
        )

    assert capped_context["page_count"] == 2
    assert capped_context["included_page_count"] == 1
    assert capped_context["truncated"] is True
    assert status == 200
    assert body["candidate_count"] == 2
    assert len(body["created_fact_ids"]) == 2
    assert body["proposal_absorption"]["requested_item_count"] == 2


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


def test_wiki_fact_migration_backfills_existing_wiki_and_surfaces_future_conflict(
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

    batch_id = create_wiki_proposal(
        paths,
        title="Friday schedule candidate",
        rationale="Newer schedule source.",
        source_ids=["document:doc_source"],
        changes=[
            {
                "target_path": "concepts/test-concept.md",
                "operation": "replace_section",
                "section_name": "Summary",
                "proposed_markdown": "The Sierra final interview is scheduled for Friday.",
                "rationale": "Friday schedule.",
                "source_ids": ["document:doc_source"],
                "confidence": 0.72,
            }
        ],
        confidence=0.72,
    )
    with connection(paths.sqlite_path) as conn:
        conn.execute(
            "UPDATE wiki_change_batches SET created_at = ? WHERE id = ?",
            ("2026-05-26T00:00:00+00:00", batch_id),
        )

    with running_ui(paths) as (host, port, token):
        absorb_status, absorbed = request_json(
            host,
            port,
            token,
            "POST",
            "/api/wiki/proposal-packets/facts",
            {"group_by": "topic", "packet_id": "topic:concepts"},
        )

    assert absorb_status == 200
    assert absorbed["resolved"]["created_question_ids"]
    assert absorbed["dashboard"]["counts"]["by_status"]["active"] == 1
    assert absorbed["dashboard"]["counts"]["by_status"]["conflicted"] == 2
    question = absorbed["dashboard"]["open_questions"][0]
    assert [option["statement"] for option in question["options"]] == [
        "The Sierra final interview is scheduled for Friday.",
        "The Sierra final interview is scheduled for Monday.",
    ]


def test_wiki_packet_fact_absorption_creates_direct_conflict_question(
    tmp_path: Path,
) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    svc = BrainService(paths, prefer_model_embeddings=False)
    svc.init_workspace()
    insert_document(paths)
    write_concept_page(paths)
    older_batch = create_wiki_proposal(
        paths,
        title="Monday schedule candidate",
        rationale="First schedule candidate.",
        source_ids=["document:doc_source"],
        changes=[
            {
                "target_path": "concepts/test-concept.md",
                "operation": "replace_section",
                "section_name": "Summary",
                "proposed_markdown": "The Sierra final interview is scheduled for Monday.",
                "rationale": "Monday schedule.",
                "source_ids": ["document:doc_source"],
                "confidence": 0.7,
            }
        ],
        confidence=0.7,
    )
    newer_batch = create_wiki_proposal(
        paths,
        title="Friday schedule candidate",
        rationale="Second schedule candidate.",
        source_ids=["document:doc_source"],
        changes=[
            {
                "target_path": "concepts/test-concept.md",
                "operation": "replace_section",
                "section_name": "Summary",
                "proposed_markdown": "The Sierra final interview is scheduled for Friday.",
                "rationale": "Friday schedule.",
                "source_ids": ["document:doc_source"],
                "confidence": 0.72,
            }
        ],
        confidence=0.72,
    )
    with connection(paths.sqlite_path) as conn:
        conn.execute(
            "UPDATE wiki_change_batches SET created_at = ? WHERE id = ?",
            ("2026-05-24T00:00:00+00:00", older_batch),
        )
        conn.execute(
            "UPDATE wiki_change_batches SET created_at = ? WHERE id = ?",
            ("2026-05-25T00:00:00+00:00", newer_batch),
        )

    with running_ui(paths) as (host, port, token):
        status, body = request_json(
            host,
            port,
            token,
            "POST",
            "/api/wiki/proposal-packets/facts",
            {"group_by": "topic", "packet_id": "topic:concepts"},
        )
        dashboard_status, dashboard = request_json(
            host, port, token, "GET", "/api/wiki/facts"
        )
        question = dashboard["open_questions"][0]
        selected_fact_id = question["options"][0]["fact_id"]
        answer_status, answer_body = request_json(
            host,
            port,
            token,
            "POST",
            f"/api/wiki/questions/{question['id']}/answer",
            {"selected_fact_id": selected_fact_id},
        )
        repeat_status, repeat_body = request_json(
            host,
            port,
            token,
            "POST",
            "/api/wiki/proposal-packets/facts",
            {"group_by": "topic", "packet_id": "topic:concepts"},
        )

    assert status == 200
    assert len(body["created_fact_ids"]) == 2
    assert len(body["resolved"]["created_question_ids"]) == 1
    assert body["resolved"]["auto_merged"] == 0
    assert dashboard_status == 200
    assert dashboard["counts"]["by_status"]["conflicted"] == 2
    assert question["question"].startswith("What is currently true")
    assert [option["statement"] for option in question["options"]] == [
        "The Sierra final interview is scheduled for Friday.",
        "The Sierra final interview is scheduled for Monday.",
    ]
    assert answer_status == 200
    assert answer_body["question"]["status"] == "answered"
    assert answer_body["dashboard"]["counts"]["by_status"]["active"] == 1
    assert answer_body["dashboard"]["counts"]["by_status"]["superseded"] == 1
    assert repeat_status == 200
    assert repeat_body["dashboard"]["open_questions"] == []
    assert repeat_body["dashboard"]["counts"]["by_status"]["active"] == 1
    assert repeat_body["dashboard"]["counts"]["by_status"]["superseded"] == 1


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


def test_wiki_packet_fact_absorption_routes_hightouch_variants_to_canonical_page(
    tmp_path: Path,
) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    svc = BrainService(paths, prefer_model_embeddings=False)
    svc.init_workspace()
    insert_document(paths)
    create_wiki_proposal(
        paths,
        title="Hightouch agentic CDP page",
        rationale="First duplicate target.",
        source_ids=["document:doc_source"],
        changes=[
            {
                "target_path": "career/hightouch-agentic-cdp.md",
                "operation": "create_page",
                "proposed_markdown": (
                    "---\n"
                    "title: Hightouch Agentic CDP\n"
                    "page_type: concept\n"
                    "id: hightouch-agentic-cdp\n"
                    "status: active\n"
                    "source_ids:\n"
                    "  - document:doc_source\n"
                    "related: []\n"
                    "tags: []\n"
                    "---\n\n"
                    "# Hightouch Agentic CDP\n\n"
                    "## Summary\n\nHightouch discussed an alternative PM role focused on leading a major AI and data initiative around the CDP experience.\n"
                ),
                "rationale": "Create duplicate page.",
                "source_ids": ["document:doc_source"],
                "confidence": 0.8,
            }
        ],
        confidence=0.8,
    )
    create_wiki_proposal(
        paths,
        title="Hightouch interview page",
        rationale="Second duplicate target.",
        source_ids=["document:doc_source"],
        changes=[
            {
                "target_path": "career/hightouch-interview.md",
                "operation": "create_page",
                "proposed_markdown": (
                    "---\n"
                    "title: Hightouch Interview\n"
                    "page_type: concept\n"
                    "id: hightouch-interview\n"
                    "status: active\n"
                    "source_ids:\n"
                    "  - document:doc_source\n"
                    "related: []\n"
                    "tags: []\n"
                    "---\n\n"
                    "# Hightouch Interview\n\n"
                    "## Summary\n\nThe Hightouch interview introduced an alternative PM opportunity focused on a major AI and data initiative around the CDP experience.\n"
                ),
                "rationale": "Create duplicate page.",
                "source_ids": ["document:doc_source"],
                "confidence": 0.82,
            }
        ],
        confidence=0.82,
    )

    with running_ui(paths) as (host, port, token):
        status, body = request_json(
            host,
            port,
            token,
            "POST",
            "/api/wiki/proposal-packets/facts",
            {"group_by": "topic", "packet_id": "topic:career"},
        )

    assert status == 200
    assert len(body["created_fact_ids"]) == 1
    assert len(body["updated_fact_ids"]) == 1
    assert body["auto_merged"] == 1
    assert body["dashboard"]["open_questions"] == []
    assert body["dashboard"]["counts"]["by_status"]["active"] == 1
    assert body["dashboard"]["recent_facts"][0]["page_hint"] == "career/hightouch.md"
    assert (paths.wiki / "career" / "hightouch.md").exists()


def test_human_wiki_proposal_endpoint_validates_path_and_uses_review_flow(
    tmp_path: Path,
) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    svc = BrainService(paths, prefer_model_embeddings=False)
    svc.init_workspace()
    insert_document(paths)
    page = write_concept_page(paths)

    body = {
        "title": "Human edit",
        "rationale": "Update after review.",
        "source_ids": ["document:doc_source"],
        "changes": [
            {
                "target_path": "concepts/test-concept.md",
                "operation": "replace_section",
                "section_name": "Summary",
                "proposed_markdown": "Human-authored summary.",
                "rationale": "Human-authored update.",
                "source_ids": ["document:doc_source"],
                "confidence": 1.0,
            }
        ],
        "confidence": 1.0,
    }

    with running_ui(paths) as (host, port, token):
        bad_status, _bad_body = request_json(
            host,
            port,
            token,
            "POST",
            "/api/wiki/proposals",
            {
                **body,
                "changes": [{**body["changes"][0], "target_path": "../outside.md"}],
            },
        )
        status, created = request_json(
            host, port, token, "POST", "/api/wiki/proposals", body
        )
        batch_id = str(created["batch_id"])
        apply_status, apply_body = request_json(
            host,
            port,
            token,
            "POST",
            f"/api/wiki/proposals/{batch_id}/approve-and-apply",
            {"questions": ["Apply?"], "answers": ["Yes."]},
        )

    assert bad_status == 400
    assert status == 200
    proposal = inspect_wiki_proposal(paths, batch_id)
    assert proposal["author"] == "human"
    assert proposal["source"] == "ui"
    assert apply_status == 200
    assert apply_body["lint"]["errors"] == []
    assert "Human-authored summary." in page.read_text(encoding="utf-8")


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
