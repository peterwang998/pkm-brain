from __future__ import annotations

import http.client
import json
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import pytest

from pkm_brain import ui_server
from pkm_brain.contracts import insert_contract_direct
from pkm_brain.cos_actions import (
    apply_action,
    decide_action,
    propose_action,
    record_action_audit,
)
from pkm_brain.cos_policy import evaluate_policy
from pkm_brain.db import connection, dumps
from pkm_brain.paths import BrainPaths
from pkm_brain.service import BrainService, memory_export_path
from pkm_brain.title_utils import CODEX_PROVIDER_PROMPT_PREFIX
from pkm_brain.ui_server import create_ui_server, ensure_ui_token
from pkm_brain.wiki import GENERATED_MARKER, lint_wiki
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


def request_raw(
    host: str,
    port: int,
    method: str,
    path: str,
    token: str | None = None,
) -> tuple[int, str, dict[str, str]]:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    conn = http.client.HTTPConnection(host, port, timeout=5)
    conn.request(method, path, headers=headers)
    response = conn.getresponse()
    response_body = response.read().decode("utf-8")
    response_headers = {key.lower(): value for key, value in response.getheaders()}
    conn.close()
    return response.status, response_body, response_headers


def test_queue_relation_context_prefers_final_resolver_disposition(
    tmp_path: Path,
) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()
    action = propose_action(
        paths,
        "fact_upsert",
        action_payload={"fact": {"statement": "Candidate fact."}},
        action_features={},
        evidence={
            "resolver_precheck": {
                "relation_classifications": [
                    {
                        "relation": "contradicts",
                        "confidence": 0.9,
                        "rationale": "stale deterministic signal",
                    }
                ],
                "resolver_judgment": {
                    "decision": "no_conflict",
                    "counterpart_fact_ids": [],
                    "rationale": "Both statements can be true.",
                },
            }
        },
        decide=False,
    )

    relation = ui_server.queue_relation_context(
        paths, {"action_id": action["id"], "context": {}}
    )

    assert relation == {
        "relation": "no conflict",
        "confidence": None,
        "rationale": "Both statements can be true.",
    }


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


def write_routing_page(
    paths: BrainPaths, *, relative_path: str, title: str, page_type: str
) -> Path:
    target = paths.wiki / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    page_id = "route-" + relative_path.removesuffix(".md").replace("/", "-")
    target.write_text(
        "---\n"
        f"title: {json.dumps(title)}\n"
        f"page_type: {page_type}\n"
        f"id: {page_id}\n"
        "status: active\n"
        "created_at: 2026-07-10\n"
        "updated_at: 2026-07-10\n"
        "source_ids: [manual:test]\n"
        "related: []\n"
        "tags: []\n"
        "---\n\n"
        f"# {title}\n\n"
        "## Summary\n\nRouting test page.\n\n"
        "## Key Points\n\n- Routing test.\n\n"
        "## Source Evidence\n\n- manual:test\n\n"
        "## Related Pages\n\n- None.\n\n"
        "## Open Questions\n\n- None.\n\n"
        "## Definition\n\nRouting test.\n\n"
        "## Why It Matters\n\nRouting test.\n\n"
        "## How It Works\n\nRouting test.\n\n"
        "## Related Decisions\n\n- None.\n\n"
        "## Notes\n\nRouting test.\n\n"
        "## Extracted Facts\n\n- None.\n",
        encoding="utf-8",
    )
    return target


def review_fact_payload(fact_id: str = "fact_review") -> dict[str, object]:
    return {
        "id": fact_id,
        "statement": "The review workflow should show enough evidence to make a decision.",
        "entity_key": "concepts:review-workflow:summary",
        "page_hint": "concepts/review-workflow.md",
        "section_hint": "Summary",
        "source_ids": ["manual:test"],
        "observed_at": "2026-07-06T10:00:00+00:00",
        "confidence": 0.91,
        "status": "active",
        "metadata": {"test": True},
        "source_spans": [
            {
                "source_id": "manual:test",
                "start_char": 0,
                "end_char": 72,
                "quote": "The review workflow should show enough evidence to make a decision.",
            }
        ],
        "evidence_quote": "The review workflow should show enough evidence to make a decision.",
        "extraction_method": "test",
    }


def existing_review_fact_payload(fact_id: str = "fact_existing") -> dict[str, object]:
    fact = review_fact_payload(fact_id)
    fact.update(
        {
            "statement": "The old review workflow did not show enough evidence.",
            "source_ids": ["manual:existing"],
            "observed_at": "2026-07-05T10:00:00+00:00",
            "evidence_quote": "The old review workflow did not show enough evidence.",
            "source_spans": [
                {
                    "source_id": "manual:existing",
                    "start_char": 0,
                    "end_char": 54,
                    "quote": "The old review workflow did not show enough evidence.",
                }
            ],
        }
    )
    return fact


def apply_existing_review_fact(paths: BrainPaths) -> dict[str, object]:
    fact = existing_review_fact_payload()
    apply_action(
        paths,
        propose_action(
            paths,
            "fact_upsert",
            action_payload={"fact": fact},
            action_features={"truth_mutation": True, "reversible": True},
            target_fact_ids=[str(fact["id"])],
            target_page_paths=[str(fact["page_hint"])],
            proposed_by="test",
            risk_tier="low",
        )["id"],
    )
    return fact


def insert_review_question_for_action(
    paths: BrainPaths, *, question_id: str, action_id: str, fact: dict[str, object]
) -> None:
    existing = existing_review_fact_payload()
    with connection(paths.sqlite_path) as conn:
        conn.execute(
            """
            INSERT INTO open_questions(
              id, kind, entity_key, page_hint, fact_ids, question, options, status,
              context, action_id, recommended_action, risk_tier, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                question_id,
                "fact_conflict_review",
                fact["entity_key"],
                fact["page_hint"],
                dumps(["fact_existing"]),
                "Candidate appears to contradict an existing nearby fact.",
                dumps(
                    [
                        {
                            "option_type": "candidate_fact",
                            "action_id": action_id,
                            **fact,
                        },
                        {
                            "option_type": "existing_fact",
                            "fact_id": existing["id"],
                            **existing,
                        },
                    ]
                ),
                "needs_human",
                dumps(
                    {
                        "action_id": action_id,
                        "counterpart_fact_ids": ["fact_existing"],
                        "relation": {
                            "relation": "contradicts",
                            "confidence": 0.94,
                            "rationale": "The candidate conflicts with the existing claim.",
                        },
                    }
                ),
                action_id,
                dumps({"action_type": "fact_upsert", "payload": {"fact": fact}}),
                "medium",
                "2026-07-06T10:01:00+00:00",
            ),
        )


def insert_unrouted_question(
    paths: BrainPaths, *, question_id: str, fact_id: str
) -> None:
    fact = review_fact_payload(fact_id)
    with connection(paths.sqlite_path) as conn:
        conn.execute(
            """
            INSERT INTO open_questions(
              id, kind, entity_key, page_hint, fact_ids, question, options, status,
              context, recommended_action, risk_tier, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                question_id,
                "unrouted_fact",
                fact["entity_key"],
                "",
                dumps([fact_id]),
                "Choose the managed page for this extracted fact.",
                dumps([{"option_type": "candidate_fact", **fact}]),
                "needs_human",
                dumps({}),
                dumps({"action_type": "rehome_fact", "payload": {"fact": fact}}),
                "low",
                "2026-07-06T10:01:00+00:00",
            ),
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
    assert "Legacy wiki packets" not in surfaces
    assert surfaces["CoS action ledger"]["searched"] is False


def test_ops_storage_endpoint_returns_managed_inventory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    app_support = tmp_path / "AppSupport"
    (app_support / "runtime" / "runtime-test").mkdir(parents=True)
    monkeypatch.setenv("PKM_BRAIN_APP_SUPPORT", str(app_support))

    with running_ui(paths) as (host, port, token):
        status, body = request_json(host, port, token, "GET", "/api/ops/storage")

    roots = {entry["key"]: entry for entry in body["roots"]}
    assert status == 200
    assert body["managed_root_bytes"] >= 0
    assert roots["app_runtimes"]["path"] == str(app_support / "runtime")
    assert roots["app_runtimes"]["item_count"] == 1


def test_v2_static_shell_serves_without_auth(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")

    with running_ui(paths) as (host, port, _token):
        root_status, root_body, root_headers = request_raw(host, port, "GET", "/")
        js_status, js_body, js_headers = request_raw(host, port, "GET", "/ui/app.js")

    assert root_status == 200
    assert '<script type="module" src="/ui/app.js"></script>' in root_body
    assert "text/html" in root_headers["content-type"]
    assert js_status == 200
    assert "Hash router" not in js_body
    assert "no-cache" in js_headers["cache-control"]


def test_v2_digest_and_queue_return_complete_review_cards(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()
    insert_document(paths)
    write_concept_page(paths, generated=True)
    fact = review_fact_payload()
    action = propose_action(
        paths,
        "fact_upsert",
        action_payload={"fact": fact},
        action_features={"truth_mutation": True, "reversible": True},
        target_fact_ids=[str(fact["id"])],
        target_page_paths=[str(fact["page_hint"])],
        proposed_by="test",
        risk_tier="medium",
    )
    insert_review_question_for_action(
        paths,
        question_id="question_v2_queue",
        action_id=action["id"],
        fact=fact,
    )
    BrainService(paths).propose_memory(
        "FactMemory", "global", "Review me from v2.", ["document:doc_source"], 0.88
    )

    with running_ui(paths) as (host, port, token):
        digest_status, digest = request_json(host, port, token, "GET", "/api/digest")
        queue_status, queue = request_json(host, port, token, "GET", "/api/queue")

    assert digest_status == 200
    assert digest["queue_counts"]["total"] == 2
    assert digest["queue_counts"]["by_kind"]["conflicts"] == 1
    assert digest["queue_counts"]["by_kind"]["memories"] == 1
    assert digest["queue_summary"]["active_total"] == 2
    assert digest["queue_summary"]["actionable_total"] == 2
    assert digest["queue_summary"]["blocked_total"] == 0
    assert queue_status == 200
    assert queue["queue_summary"]["active_total"] == queue["counts"]["total"] == 2
    assert digest["queue_summary"]["as_of"] < queue["queue_summary"]["as_of"]
    assert queue["queue_summary"]["server_pid"] > 0
    assert queue["queue_summary"]["home"] == str(paths.home)
    conflict = next(
        item for item in queue["items"] if item["id"] == "question_v2_queue"
    )
    assert conflict["group"] == "conflicts"
    assert conflict["title"] == "Review Workflow / Summary"
    assert conflict["orientation"]["entity_label"] == "Review Workflow"
    assert conflict["orientation"]["page_hint"] == "concepts/review-workflow.md"
    assert conflict["orientation"]["section_hint"] == "Summary"
    assert (
        conflict["orientation"]["candidate_observed_at"] == "2026-07-06T10:00:00+00:00"
    )
    assert (
        conflict["orientation"]["existing_observed_at"] == "2026-07-05T10:00:00+00:00"
    )
    assert conflict["orientation"]["temporal_scope"] == "atemporal_claim"
    assert conflict["candidate"]["statement"] == fact["statement"]
    assert conflict["candidate"]["source_date"] == "2026-07-06T10:00:00+00:00"
    assert conflict["candidate"]["source_date_basis"] == "observed_at"
    assert conflict["approvable"] is True
    assert (
        conflict["counterparts"][0]["statement"]
        == "The old review workflow did not show enough evidence."
    )
    memory = next(item for item in queue["items"] if item["group"] == "memories")
    assert memory["memory"]["source_documents"][0]["source_id"] == "document:doc_source"
    assert (
        memory["memory"]["source_documents"][0]["created_at"]
        == "2026-05-25T00:00:00+00:00"
    )


def test_v2_queue_defers_future_work_beyond_daily_admission_budget(
    tmp_path: Path,
) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()

    with running_ui(paths) as (host, port, token):
        initial_status, initial = request_json(
            host, port, token, "GET", "/api/queue"
        )
        assert initial_status == 200
        assert initial["queue_summary"]["active_total"] == 0

        with connection(paths.sqlite_path) as conn:
            conn.executemany(
                """
                INSERT INTO memories(
                  id, memory_type, scope, content, confidence, source_ids,
                  status, created_at, updated_at
                ) VALUES (?, 'FactMemory', 'global', ?, 0.8, ?, 'proposed', ?, ?)
                """,
                [
                    (
                        f"memory_budget_{index:02d}",
                        f"Synthetic review memory {index}.",
                        dumps(["document:synthetic"]),
                        f"2026-07-11T10:00:{index:02d}+00:00",
                        f"2026-07-11T10:00:{index:02d}+00:00",
                    )
                    for index in range(30)
                ],
            )

        active_status, active = request_json(
            host, port, token, "GET", "/api/queue"
        )
        deferred_status, deferred = request_json(
            host, port, token, "GET", "/api/queue?state=deferred"
        )

    assert active_status == deferred_status == 200
    assert active["queue_summary"]["active_total"] == 25
    assert active["queue_summary"]["deferred_total"] == 5
    assert active["queue_summary"]["daily_admission_limit"] == 25
    assert active["queue_summary"]["admitted_today"] == 25
    assert active["total"] == active["counts"]["total"] == 25
    assert deferred["state"] == "deferred"
    assert deferred["total"] == deferred["counts"]["total"] == 5
    assert len(deferred["items"]) == 5
    assert {item["admission_state"] for item in deferred["items"]} == {"deferred"}


def test_v2_queue_blocks_candidate_less_conflict_and_direct_decision(
    tmp_path: Path,
) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()
    existing = existing_review_fact_payload("fact_only_existing")
    with connection(paths.sqlite_path) as conn:
        conn.execute(
            """
            INSERT INTO open_questions(
              id, kind, entity_key, page_hint, fact_ids, question, options, status,
              context, recommended_action, risk_tier, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "question_missing_candidate",
                "fact_conflict_review",
                existing["entity_key"],
                existing["page_hint"],
                dumps([existing["id"]]),
                "An incomplete historical conflict must be repaired.",
                dumps([{"option_type": "existing_fact", **existing}]),
                "needs_human",
                dumps(
                    {
                        "relation": {
                            "relation": "contradicts",
                            "confidence": 0.9,
                        }
                    }
                ),
                dumps({}),
                "medium",
                "2026-07-06T10:01:00+00:00",
            ),
        )

    with running_ui(paths) as (host, port, token):
        queue_status, queue = request_json(host, port, token, "GET", "/api/queue")
        blocked_status, blocked_queue = request_json(
            host, port, token, "GET", "/api/queue?state=blocked"
        )
        decision_status, decision = request_json(
            host,
            port,
            token,
            "POST",
            "/api/queue/question_missing_candidate/decision",
            {"decision": "candidate_wins"},
        )

    assert queue_status == 200
    assert queue["state"] == "actionable"
    assert queue["total"] == 0
    assert queue["items"] == []
    assert blocked_status == 200
    assert blocked_queue["state"] == "blocked"
    assert blocked_queue["total"] == 1
    item = blocked_queue["items"][0]
    assert item["approvable"] is False
    assert item["blocking_code"] == "missing_fact"
    assert blocked_queue["queue_summary"]["active_total"] == 1
    assert blocked_queue["queue_summary"]["actionable_total"] == 0
    assert blocked_queue["queue_summary"]["blocked_total"] == 1
    assert decision_status == 400
    assert "Candidate fact payload is unavailable" in decision["error"]
    with connection(paths.sqlite_path) as conn:
        status = conn.execute(
            "SELECT status FROM open_questions WHERE id = ?",
            ("question_missing_candidate",),
        ).fetchone()[0]
    assert status == "needs_human"


def test_v2_queue_models_legacy_conflicts_as_selectable_alternatives(
    tmp_path: Path,
) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()
    existing = apply_existing_review_fact(paths)
    latest = review_fact_payload("fact_alternative_latest")
    latest["statement"] = "The current review workflow shows complete evidence."
    latest["evidence_quote"] = latest["statement"]
    latest["source_spans"] = [
        {
            "source_id": "manual:test",
            "start_char": 0,
            "end_char": len(str(latest["statement"])),
            "quote": latest["statement"],
        }
    ]
    supporting = review_fact_payload("fact_alternative_supporting")
    supporting["statement"] = "The review workflow also records source dates."
    supporting["evidence_quote"] = supporting["statement"]
    supporting["source_spans"] = [
        {
            "source_id": "manual:test",
            "start_char": 0,
            "end_char": len(str(supporting["statement"])),
            "quote": supporting["statement"],
        }
    ]
    apply_action(
        paths,
        propose_action(
            paths,
            "fact_upsert",
            action_payload={"fact": latest},
            action_features={"truth_mutation": True, "reversible": True},
            target_fact_ids=[str(latest["id"])],
            target_page_paths=[str(latest["page_hint"])],
            proposed_by="test",
            risk_tier="low",
        )["id"],
    )
    apply_action(
        paths,
        propose_action(
            paths,
            "fact_upsert",
            action_payload={"fact": supporting},
            action_features={"truth_mutation": True, "reversible": True},
            target_fact_ids=[str(supporting["id"])],
            target_page_paths=[str(supporting["page_hint"])],
            proposed_by="test",
            risk_tier="low",
        )["id"],
    )
    options = [
        {
            "fact_id": fact["id"],
            "statement": fact["statement"],
            "confidence": fact["confidence"],
            "observed_at": fact["observed_at"],
            "source_ids": fact["source_ids"],
        }
        for fact in (latest, existing, supporting)
    ]
    with connection(paths.sqlite_path) as conn:
        conn.execute(
            """
            INSERT INTO open_questions(
              id, kind, entity_key, page_hint, fact_ids, question, options, status,
              context, risk_tier, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "question_legacy_alternatives",
                "conflict",
                latest["entity_key"],
                latest["page_hint"],
                dumps([latest["id"], existing["id"], supporting["id"]]),
                "What is currently true for the review workflow?",
                dumps(options),
                "open",
                dumps({"conflict_group_id": "factconflict_legacy"}),
                "medium",
                "2026-07-06T10:01:00+00:00",
            ),
        )

    with running_ui(paths) as (host, port, token):
        queue_status, queue = request_json(
            host, port, token, "GET", "/api/queue?kind=conflicts"
        )
        invalid_status, invalid = request_json(
            host,
            port,
            token,
            "POST",
            "/api/queue/question_legacy_alternatives/decision",
            {"decision": "candidate_wins"},
        )
        empty_status, empty = request_json(
            host,
            port,
            token,
            "POST",
            "/api/queue/question_legacy_alternatives/decision",
            {"decision": "select_facts", "selected_fact_ids": []},
        )
        outside_status, outside = request_json(
            host,
            port,
            token,
            "POST",
            "/api/queue/question_legacy_alternatives/decision",
            {"decision": "select_facts", "selected_fact_ids": ["fact_outside"]},
        )
        decision_status, decision = request_json(
            host,
            port,
            token,
            "POST",
            "/api/queue/question_legacy_alternatives/decision",
            {
                "decision": "select_facts",
                "selected_fact_ids": [latest["id"], supporting["id"]],
            },
        )

    assert queue_status == 200
    item = queue["items"][0]
    assert item["comparison_mode"] == "alternatives"
    assert item["candidate"] is None
    assert item["counterparts"] == []
    assert [fact["id"] for fact in item["alternatives"]] == [
        latest["id"],
        existing["id"],
        supporting["id"],
    ]
    assert item["orientation"]["relation"] == "contested"
    assert item["approvable"] is True
    assert invalid_status == 400
    assert "one or more facts" in invalid["error"]
    assert empty_status == 400
    assert "at least one fact" in empty["error"]
    assert outside_status == 400
    assert "outside the question" in outside["error"]
    assert decision_status == 200
    assert decision["queue_summary"]["active_total"] == 0
    with connection(paths.sqlite_path) as conn:
        statuses = {
            row["id"]: (row["status"], row["confirmed_by_user"])
            for row in conn.execute(
                "SELECT id, status, confirmed_by_user FROM facts WHERE id IN (?, ?, ?)",
                (latest["id"], existing["id"], supporting["id"]),
            )
        }
    assert statuses == {
        latest["id"]: ("active", 1),
        existing["id"]: ("superseded", 0),
        supporting["id"]: ("active", 1),
    }


def test_v2_queue_extraction_anomaly_uses_alert_decisions(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()
    with connection(paths.sqlite_path) as conn:
        conn.execute(
            """
            INSERT INTO open_questions(
              id, kind, question, options, status, context, recommended_action,
              risk_tier, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "question_extraction_alert",
                "document_extraction_anomaly",
                "Critic blocked 3/4 extracted facts for Example Interview.",
                dumps([]),
                "needs_human",
                dumps(
                    {
                        "document_id": "doc_example",
                        "title": "Example Interview",
                        "reviewed_action_ids": [
                            "action_1",
                            "action_2",
                            "action_3",
                            "action_4",
                        ],
                        "blocked_action_ids": ["action_1", "action_2", "action_3"],
                        "block_rate": 0.75,
                    }
                ),
                dumps({"action_type": "review_document_extraction"}),
                "medium",
                "2026-07-10T18:00:00+00:00",
            ),
        )

    with running_ui(paths) as (host, port, token):
        queue_status, queue = request_json(
            host, port, token, "GET", "/api/queue?kind=anomalies"
        )
        decision_status, decision = request_json(
            host,
            port,
            token,
            "POST",
            "/api/queue/question_extraction_alert/decision",
            {"decision": "acknowledge"},
        )
        undo_status, undo = request_json(
            host,
            port,
            token,
            "POST",
            "/api/queue/undo",
            {"undo_handle": decision["undo_handle"]},
        )

    assert queue_status == 200
    item = queue["items"][0]
    assert item["title"] == "Extraction quality: Example Interview"
    assert item["anomaly"] == {
        "document_id": "doc_example",
        "document_title": "Example Interview",
        "reviewed_count": 4,
        "blocked_count": 3,
        "block_rate": 0.75,
    }
    assert decision_status == 200
    assert decision["result"]["question"]["status"] == "answered"
    assert decision["result"]["question"]["answer"]["decision"] == "acknowledged"
    assert undo_status == 200
    with connection(paths.sqlite_path) as conn:
        status = conn.execute(
            "SELECT status FROM open_questions WHERE id = ?",
            ("question_extraction_alert",),
        ).fetchone()[0]
    assert status == "needs_human"


def test_v2_queue_audit_card_surfaces_finding_and_applied_fact(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()
    fact = review_fact_payload("fact_audit_card")
    action = apply_action(
        paths,
        propose_action(
            paths,
            "fact_upsert",
            action_payload={"fact": fact},
            action_features={"truth_mutation": False, "reversible": True},
            target_fact_ids=[fact["id"]],
            target_page_paths=[fact["page_hint"]],
            proposed_by="test",
            risk_tier="low",
        )["id"],
    )
    finding = "The applied statement overstates what the quoted evidence supports."
    record_action_audit(
        paths,
        action["id"],
        "sampled_bad",
        metadata={
            "rationale": finding,
            "provider": "codex",
            "model": "gpt-5.6-sol",
        },
    )

    with running_ui(paths) as (host, port, token):
        queue_status, queue = request_json(
            host, port, token, "GET", "/api/queue?kind=audit"
        )
        decision_status, decision = request_json(
            host,
            port,
            token,
            "POST",
            f"/api/queue/{action['id']}/decision",
            {"decision": "mark_ok"},
        )

    assert queue_status == 200
    item = queue["items"][0]
    assert item["title"] == f"Audit finding: {fact['statement']}"
    assert item["summary"] == finding
    assert item["candidate"]["id"] == fact["id"]
    assert item["audit"]["rationale"] == finding
    assert item["audit"]["model"] == "gpt-5.6-sol"
    assert item["audit"]["action_status"] == "applied"
    assert item["audit"]["affected_fact_count"] == 1
    assert item["audit"]["revertible"] is True
    assert item["approvable"] is True
    assert decision_status == 200
    assert decision["result"]["action"]["audit_status"] == "sampled_ok"


def test_v2_queue_keeps_active_audited_fact_after_related_state_drift(
    tmp_path: Path,
) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()
    counterpart = review_fact_payload("fact_audit_context")
    counterpart["statement"] = "A nearby fact supplied comparison context."
    counterpart["evidence_quote"] = counterpart["statement"]
    apply_action(
        paths,
        propose_action(
            paths,
            "fact_upsert",
            action_payload={"fact": counterpart},
            action_features={"truth_mutation": False, "reversible": True},
            target_fact_ids=[counterpart["id"]],
            target_page_paths=[counterpart["page_hint"]],
            proposed_by="test",
            risk_tier="low",
        )["id"],
    )
    fact = review_fact_payload("fact_audit_drift")
    action = apply_action(
        paths,
        propose_action(
            paths,
            "fact_upsert",
            action_payload={"fact": fact},
            action_features={"truth_mutation": False, "reversible": True},
            target_fact_ids=[counterpart["id"], fact["id"]],
            target_page_paths=[fact["page_hint"]],
            proposed_by="test",
            risk_tier="low",
        )["id"],
    )
    record_action_audit(
        paths,
        action["id"],
        "sampled_bad",
        metadata={"rationale": "The active fact still needs review."},
    )
    with connection(paths.sqlite_path) as conn:
        conn.execute(
            "UPDATE facts SET page_hint = 'concepts/current-home.md' WHERE id = ?",
            (fact["id"],),
        )

    with running_ui(paths) as (host, port, token):
        queue_status, queue = request_json(
            host, port, token, "GET", "/api/queue?kind=audit"
        )
        revert_status, revert = request_json(
            host,
            port,
            token,
            "POST",
            f"/api/queue/{action['id']}/decision",
            {"decision": "revert"},
        )
        undo_status, _ = request_json(
            host,
            port,
            token,
            "POST",
            "/api/queue/undo",
            {"undo_handle": revert["undo_handle"]},
        )

    assert queue_status == 200
    assert queue["total"] == 1
    item = queue["items"][0]
    assert item["candidate"]["page_hint"] == "concepts/current-home.md"
    assert item["audit"]["affected_fact_count"] == 1
    assert [row["id"] for row in item["audit"]["affected_facts"]] == [fact["id"]]
    assert item["audit"]["revertible"] is True
    assert item["audit"]["revert_mode"] == "reject_current_fact"
    assert (
        item["audit"]["reviewability_reason"]
        == "audited_fact_still_active_after_related_drift"
    )
    assert revert_status == 200
    assert revert["result"]["action"]["audit_status"] == "remediated"
    assert revert["result"]["correction_action"]["action_type"] == "fact_supersede"
    assert undo_status == 200
    with connection(paths.sqlite_path) as conn:
        current = conn.execute(
            "SELECT status, audit_status FROM cos_actions WHERE id = ?", (action["id"],)
        ).fetchone()
        current_fact = conn.execute(
            "SELECT status FROM facts WHERE id = ?", (fact["id"],)
        ).fetchone()
    assert current["status"] == "applied"
    assert current["audit_status"] == "sampled_bad"
    assert current_fact["status"] == "active"


def test_v2_queue_topology_audit_shows_applied_change_and_hides_after_drift(
    tmp_path: Path,
) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()
    destination = "concepts/merge-destination.md"
    source = "concepts/merge-source.md"
    for fact_id, page_hint, statement in (
        ("fact_destination_a", destination, "Destination fact A."),
        ("fact_destination_b", destination, "Destination fact B."),
        ("fact_source", source, "Source fact moved by the audited merge."),
    ):
        fact = review_fact_payload(fact_id)
        fact.update(
            {
                "statement": statement,
                "evidence_quote": statement,
                "page_hint": page_hint,
                "entity_key": f"concepts:{fact_id}:summary",
            }
        )
        apply_action(
            paths,
            propose_action(
                paths,
                "fact_upsert",
                action_payload={"fact": fact},
                action_features={"truth_mutation": False, "reversible": True},
                target_fact_ids=[fact_id],
                target_page_paths=[page_hint],
                proposed_by="test",
                risk_tier="low",
            )["id"],
        )
    candidate_key = f"page_merge:{destination},{source}:"
    action = apply_action(
        paths,
        propose_action(
            paths,
            "page_merge",
            action_payload={
                "candidate": {
                    "action_type": "page_merge",
                    "candidate_key": candidate_key,
                    "page_hints": [destination, source],
                    "reason": "near-duplicate pages",
                }
            },
            action_features={
                "candidate_key": candidate_key,
                "reversible": True,
            },
            target_page_paths=[destination, source],
            proposed_by="test",
            risk_tier="medium",
        )["id"],
    )
    finding = "The merge did not have enough policy evidence."
    record_action_audit(
        paths,
        action["id"],
        "sampled_bad",
        metadata={
            "rationale": finding,
            "provider": "codex",
            "model": "gpt-5.6-sol",
        },
    )

    with running_ui(paths) as (host, port, token):
        queue_status, queue = request_json(
            host, port, token, "GET", "/api/queue?kind=audit"
        )
        with connection(paths.sqlite_path) as conn:
            conn.execute(
                "UPDATE facts SET section_hint = 'Drifted' WHERE id = 'fact_source'"
            )
        stale_status, stale_queue = request_json(
            host, port, token, "GET", "/api/queue?kind=audit"
        )
        decision_status, _ = request_json(
            host,
            port,
            token,
            "POST",
            f"/api/queue/{action['id']}/decision",
            {"decision": "revert"},
        )

    assert queue_status == 200
    assert queue["total"] == 1
    item = queue["items"][0]
    assert item["title"] == f"Audit finding: Merge {source} into {destination}"
    assert item["summary"] == finding
    assert item["topology"]["target_label"] == f"{source} into {destination}"
    assert item["topology"]["merge_destination_label"] == destination
    assert item["topology"]["merge_source_labels"] == [source]
    assert item["audit"]["affected_fact_count"] == 1
    assert item["audit"]["affected_facts"][0]["id"] == "fact_source"
    assert item["audit"]["affected_facts"][0]["statement"] == (
        "Source fact moved by the audited merge."
    )
    assert item["audit"]["revertible"] is True
    assert item["proposal"]["candidate"]["candidate_key"] == candidate_key
    assert item["approvable"] is True
    assert stale_status == 200
    assert stale_queue["total"] == 0
    assert stale_queue["queue_summary"]["by_kind"].get("audit") is None
    assert decision_status == 404
    with connection(paths.sqlite_path) as conn:
        current = conn.execute(
            "SELECT status, audit_status FROM cos_actions WHERE id = ?", (action["id"],)
        ).fetchone()
    assert current["status"] == "applied"
    assert current["audit_status"] == "sampled_bad"


def test_v2_queue_applied_entity_merge_audit_accepts_expected_post_state(
    tmp_path: Path,
) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()
    with connection(paths.sqlite_path) as conn:
        conn.executemany(
            """
            INSERT INTO entities(id, name, entity_type, aliases, status, source_ids, created_at)
            VALUES (?, ?, 'organization', '[]', 'active', '[]', ?)
            """,
            [
                ("entity_brain", "Brain", "2026-07-12T10:00:00+00:00"),
                ("entity_pkm_brain", "PKM Brain", "2026-07-12T10:00:00+00:00"),
            ],
        )
    action = apply_action(
        paths,
        propose_action(
            paths,
            "entity_merge",
            action_payload={
                "canonical_entity_id": "entity_brain",
                "merged_entity_ids": ["entity_pkm_brain"],
            },
            action_features={"reversible": True},
            proposed_by="test",
            risk_tier="medium",
        )["id"],
    )
    record_action_audit(
        paths,
        action["id"],
        "sampled_bad",
        metadata={"rationale": "The applied merge needs identity review."},
    )

    with running_ui(paths) as (host, port, token):
        status, queue = request_json(host, port, token, "GET", "/api/queue?kind=audit")

    assert status == 200
    assert queue["total"] == 1
    item = queue["items"][0]
    assert item["title"] == "Audit finding: Merge PKM Brain into Brain"
    assert item["topology"]["target_label"] == "PKM Brain into Brain"
    assert item["topology"]["entity_statuses"] == {
        "entity_brain": "active",
        "entity_pkm_brain": "merged",
    }
    assert item["audit"]["revertible"] is True
    assert item["approvable"] is True
    assert item["blocking_code"] is None


def test_v2_queue_contract_only_merge_audit_shows_direction_and_contract_state(
    tmp_path: Path,
) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()
    destination = "concepts/alpha.md"
    source = "concepts/alpha-details.md"
    with connection(paths.sqlite_path) as conn:
        for contract_id, page_hint in (
            ("contract_alpha", destination),
            ("contract_alpha_details", source),
        ):
            insert_contract_direct(
                conn,
                {
                    "id": contract_id,
                    "page_hint": page_hint,
                    "canonical_entity": page_hint,
                    "page_scope": f"Facts for {page_hint}.",
                    "retrieval_purpose": f"Retrieve {page_hint}.",
                    "what_belongs_here": "Alpha facts.",
                    "what_does_not_belong_here": "Unrelated facts.",
                    "freshness_policy": "Refresh when facts change.",
                    "status": "active",
                },
            )
    candidate_key = f"page_merge:{destination},{source}:"
    action = apply_action(
        paths,
        propose_action(
            paths,
            "page_merge",
            action_payload={
                "candidate": {
                    "action_type": "page_merge",
                    "candidate_key": candidate_key,
                    "page_hints": [destination, source],
                    "reason": "similar page scopes",
                }
            },
            action_features={"candidate_key": candidate_key, "reversible": True},
            target_page_paths=[destination, source],
            target_contract_ids=["contract_alpha", "contract_alpha_details"],
            proposed_by="test",
            risk_tier="medium",
        )["id"],
    )
    record_action_audit(
        paths,
        action["id"],
        "sampled_bad",
        metadata={"rationale": "Contract-only merge needs review."},
    )

    with running_ui(paths) as (host, port, token):
        queue_status, queue = request_json(
            host, port, token, "GET", "/api/queue?kind=audit"
        )

    assert queue_status == 200
    item = queue["items"][0]
    assert item["title"] == f"Audit finding: Merge {source} into {destination}"
    assert item["topology"]["target_label"] == f"{source} into {destination}"
    assert item["topology"]["page_statuses"] == {
        destination: "active",
        source: "superseded",
    }
    assert item["audit"]["affected_fact_count"] == 0
    assert item["audit"]["affected_page_count"] == 2
    assert item["audit"]["affected_contract_count"] == 2
    assert item["audit"]["revertible"] is True
    assert item["approvable"] is True


def test_v2_queue_resolves_chunk_provenance_to_document_source_date(
    tmp_path: Path,
) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()
    insert_document(paths)
    with connection(paths.sqlite_path) as conn:
        conn.execute(
            """
            INSERT INTO chunks(
              id, document_id, chunk_index, corpus_type, text, heading_path,
              start_offset, end_offset, token_count, content_hash, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "chunk_review_source",
                "doc_source",
                0,
                "raw",
                "Source evidence body.",
                "Summary",
                0,
                21,
                4,
                "chunk-hash",
                "2026-05-25T00:00:00+00:00",
            ),
        )
    fact = review_fact_payload("fact_chunk_source")
    fact["observed_at"] = None
    fact["source_ids"] = ["chunk:chunk_review_source"]
    fact["source_spans"] = [
        {
            "chunk_id": "chunk_review_source",
            "start_char": 0,
            "end_char": 21,
            "quote": "Source evidence body.",
        }
    ]
    action = propose_action(
        paths,
        "fact_upsert",
        action_payload={"fact": fact},
        action_features={"truth_mutation": True, "reversible": True},
        target_fact_ids=[str(fact["id"])],
        target_page_paths=[str(fact["page_hint"])],
        proposed_by="test",
        risk_tier="medium",
    )
    insert_review_question_for_action(
        paths,
        question_id="question_chunk_source",
        action_id=action["id"],
        fact=fact,
    )

    with running_ui(paths) as (host, port, token):
        status, queue = request_json(host, port, token, "GET", "/api/queue")

    assert status == 200
    candidate = queue["items"][0]["candidate"]
    assert candidate["source_date"] == "2026-05-25T00:00:00+00:00"
    assert candidate["source_date_basis"] == "source_created_at"
    assert candidate["source_documents"][0]["title"] == "Source Evidence"
    assert candidate["source_documents"][0]["source_refs"] == [
        "chunk:chunk_review_source"
    ]


def test_v2_queue_filters_and_paginates_before_expensive_enrichment(
    tmp_path: Path, monkeypatch
) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()
    for index in range(8):
        insert_unrouted_question(
            paths,
            question_id=f"question_unrouted_{index}",
            fact_id=f"fact_unrouted_{index}",
        )
    BrainService(paths).propose_memory(
        "FactMemory", "global", "Only the proposed memory should be loaded.", [], 0.8
    )

    def fail_route_enrichment(
        *_args: object, **_kwargs: object
    ) -> list[dict[str, object]]:
        raise AssertionError(
            "route candidates should not be built for filtered-out rows"
        )

    monkeypatch.setattr(ui_server, "route_candidates_for_fact", fail_route_enrichment)

    with running_ui(paths) as (host, port, token):
        status, queue = request_json(
            host,
            port,
            token,
            "GET",
            "/api/queue?kind=proposed_memory&state=all&limit=1",
        )

    assert status == 200
    assert queue["total"] == 1
    assert len(queue["items"]) == 1
    assert queue["items"][0]["group"] == "memories"


def test_v2_queue_priority_sort_keeps_newest_item_first_within_tier(
    tmp_path: Path,
) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()
    insert_unrouted_question(
        paths, question_id="question_priority_old", fact_id="fact_priority_old"
    )
    insert_unrouted_question(
        paths, question_id="question_priority_new", fact_id="fact_priority_new"
    )
    with connection(paths.sqlite_path) as conn:
        conn.execute(
            "UPDATE open_questions SET created_at = ? WHERE id = ?",
            ("2026-07-05T10:00:00+00:00", "question_priority_old"),
        )
        conn.execute(
            "UPDATE open_questions SET created_at = ? WHERE id = ?",
            ("2026-07-06T10:00:00+00:00", "question_priority_new"),
        )

    with running_ui(paths) as (host, port, token):
        status, queue = request_json(
            host,
            port,
            token,
            "GET",
            "/api/queue?kind=unrouted&sort=priority",
        )

    assert status == 200
    assert [item["id"] for item in queue["items"]] == [
        "question_priority_new",
        "question_priority_old",
    ]


def test_v2_queue_policy_escalation_uses_human_readable_summary(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()
    insert_document(paths)
    action = decide_action(
        paths,
        propose_action(
            paths,
            "fact_upsert",
            action_payload={
                "fact": {
                    "id": "fact_policy_queue",
                    "statement": "Policy-gated fact should explain itself.",
                    "page_hint": "concepts/policy.md",
                    "source_ids": ["document:doc_source"],
                    "evidence_quote": "Policy-gated fact should explain itself.",
                    "confidence": 0.7,
                }
            },
            action_features={
                "truth_mutation": False,
                "risk_score": 0.4,
                "risk_tier": "medium",
            },
            target_page_paths=["concepts/policy.md"],
        )["id"],
    )
    with connection(paths.sqlite_path) as conn:
        conn.execute(
            """
            UPDATE open_questions
            SET question = 'matched policy policy_v1_low_l1_critic'
            WHERE action_id = ?
            """,
            (action["id"],),
        )

    with running_ui(paths) as (host, port, token):
        status, queue = request_json(
            host, port, token, "GET", "/api/queue?kind=policy_escalation"
        )

    assert status == 200
    item = queue["items"][0]
    assert item["kind"] == "policy_escalation"
    assert item["action"]["action_type"] == "fact_upsert"
    assert item["candidate"]["statement"] == "Policy-gated fact should explain itself."
    assert (
        item["candidate"]["evidence_quote"]
        == "Policy-gated fact should explain itself."
    )
    assert item["candidate"]["source_documents"][0]["title"] == "Source Evidence"
    assert "Fact upsert matched" in item["summary"]
    assert "review level is" in item["summary"]
    assert "matched policy policy_" not in item["summary"]


def test_v2_queue_hydrates_thin_existing_fact_option(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()
    apply_existing_review_fact(paths)
    fact = review_fact_payload("fact_thin_counterpart")
    action = propose_action(
        paths,
        "fact_upsert",
        action_payload={"fact": fact},
        target_fact_ids=[str(fact["id"])],
    )
    insert_review_question_for_action(
        paths,
        question_id="question_thin_counterpart",
        action_id=action["id"],
        fact=fact,
    )
    with connection(paths.sqlite_path) as conn:
        row = conn.execute(
            "SELECT options FROM open_questions WHERE id = 'question_thin_counterpart'"
        ).fetchone()
        options = json.loads(row["options"])
        options[1] = {
            "option_type": "existing_fact",
            "fact_id": "fact_existing",
            "statement": "The old review workflow did not show enough evidence.",
        }
        conn.execute(
            "UPDATE open_questions SET options = ? WHERE id = 'question_thin_counterpart'",
            (dumps(options),),
        )

    with running_ui(paths) as (host, port, token):
        status, queue = request_json(
            host, port, token, "GET", "/api/queue?kind=conflicts"
        )

    assert status == 200
    existing = queue["items"][0]["counterparts"][0]
    assert (
        existing["evidence_quote"]
        == "The old review workflow did not show enough evidence."
    )
    assert existing["source_ids"] == ["manual:existing"]


def test_v2_queue_page_split_preview_and_candidate_deduplication(
    tmp_path: Path,
) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()
    page_hint = "people/alex.md"
    for index, section in enumerate(["Career", "Projects", "Preferences"]):
        fact = review_fact_payload(f"fact_split_{index}")
        fact.update(
            {
                "statement": f"Alex has a {section.lower()} fact.",
                "page_hint": page_hint,
                "section_hint": section,
            }
        )
        apply_action(
            paths,
            propose_action(
                paths,
                "fact_upsert",
                action_payload={"fact": fact},
                target_fact_ids=[str(fact["id"])],
                target_page_paths=[page_hint],
            )["id"],
        )
    candidate = {
        "action_type": "page_split",
        "page_hints": [page_hint],
        "candidate_key": f"page_split:{page_hint}",
        "reason": "dense page has active facts across multiple sections",
    }
    primary = propose_action(
        paths,
        "page_split",
        action_payload={"candidate": candidate},
        action_features={
            "candidate_key": candidate["candidate_key"],
            "reversible": True,
        },
        target_page_paths=[page_hint],
    )
    duplicate = propose_action(
        paths,
        "page_split",
        action_payload={"candidate": candidate},
        action_features={"reversible": True},
        target_page_paths=[page_hint],
    )
    with connection(paths.sqlite_path) as conn:
        conn.execute(
            """
            INSERT INTO open_questions(
              id, kind, page_hint, fact_ids, question, options, status,
              context, action_id, recommended_action, risk_tier, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "question_split_policy",
                "policy_escalation",
                page_hint,
                "[]",
                "Page split requires review.",
                "[]",
                "needs_human",
                dumps({"action_id": primary["id"]}),
                primary["id"],
                dumps(
                    {"action_type": "page_split", "payload": {"candidate": candidate}}
                ),
                "medium",
                "2026-07-09T12:00:00+00:00",
            ),
        )

    with running_ui(paths) as (host, port, token):
        status, queue = request_json(
            host, port, token, "GET", "/api/queue?kind=topology"
        )

    assert primary["id"] != duplicate["id"]
    assert status == 200
    assert queue["total"] == 1
    assert len(queue["items"]) == 1
    item = queue["items"][0]
    assert item["title"] == "Split page: people/alex.md"
    assert item["summary"] == "dense page has active facts across multiple sections"
    preview = item["topology"]["split_preview"]
    assert preview["movable_fact_count"] == 3
    assert preview["resulting_page_count"] == 4
    assert [child["page_hint"] for child in preview["children"]] == [
        "people/alex-career.md",
        "people/alex-preferences.md",
        "people/alex-projects.md",
    ]
    assert all(child["representative_facts"] for child in preview["children"])


def test_v2_queue_unrouted_inbox_batch_filter_and_decision(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()
    with connection(paths.sqlite_path) as conn:
        conn.execute(
            """
            INSERT INTO open_questions(
              id, kind, page_hint, fact_ids, question, options, status,
              context, recommended_action, risk_tier, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "question_inbox_batch",
                "unrouted_inbox_batch",
                "projects/pkm-brain.md",
                "[]",
                "Three facts were filed to Inbox.",
                "[]",
                "needs_human",
                dumps(
                    {
                        "page_hint": "projects/pkm-brain.md",
                        "section": "Inbox",
                        "source_question_ids": ["q1", "q2", "q3"],
                    }
                ),
                dumps({"action_type": "review_unrouted_inbox_batch"}),
                "low",
                "2026-07-09T12:00:00+00:00",
            ),
        )

    with running_ui(paths) as (host, port, token):
        status, queue = request_json(
            host, port, token, "GET", "/api/queue?kind=unrouted"
        )
        decision_status, decision = request_json(
            host,
            port,
            token,
            "POST",
            "/api/queue/question_inbox_batch/decision",
            {"decision": "reviewed"},
        )

    assert status == 200
    assert queue["total"] == 1
    assert queue["items"][0]["kind"] == "unrouted_inbox_batch"
    assert queue["items"][0]["group"] == "unrouted"
    assert decision_status == 200
    assert decision["result"]["question"]["status"] == "answered"
    assert decision["result"]["question"]["answer"]["decision"] == "reviewed"


def test_v2_queue_route_candidates_exclude_internal_and_nonsemantic_pages(
    tmp_path: Path,
) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()
    long_valid_title = "Review Workflow " + ("routing details " * 20)
    write_routing_page(
        paths,
        relative_path="concepts/review-workflow.md",
        title=long_valid_title,
        page_type="concept",
    )
    write_routing_page(
        paths,
        relative_path="references/agent_session_log/provider-session.md",
        title=CODEX_PROVIDER_PROMPT_PREFIX + " Review workflow routing details.",
        page_type="reference",
    )
    write_routing_page(
        paths,
        relative_path="concepts/index.md",
        title="Review Workflow Index",
        page_type="index",
    )
    write_routing_page(
        paths,
        relative_path="concepts/internal-provider-session.md",
        title=CODEX_PROVIDER_PROMPT_PREFIX + " Review workflow routing details.",
        page_type="concept",
    )
    write_routing_page(
        paths,
        relative_path="inbox/review-workflow.md",
        title="Review Workflow Inbox Residue",
        page_type="concept",
    )
    lint_wiki(paths)
    insert_unrouted_question(
        paths,
        question_id="question_routing_sanitizer",
        fact_id="fact_routing_sanitizer",
    )

    direct_candidates = ui_server.route_candidates_for_fact(
        paths, review_fact_payload("fact_routing_sanitizer")
    )
    with running_ui(paths) as (host, port, token):
        status, queue = request_json(
            host, port, token, "GET", "/api/queue?kind=unrouted"
        )
        route_status, route_pages = request_json(
            host, port, token, "GET", "/api/wiki/pages?routable=1"
        )

    assert status == 200, queue
    candidates = queue["items"][0]["route_candidates"]
    assert [candidate["page_hint"] for candidate in candidates] == [
        "concepts/review-workflow.md"
    ]
    assert direct_candidates == candidates
    assert len(candidates[0]["title"]) <= 120
    assert not candidates[0]["title"].startswith(CODEX_PROVIDER_PROMPT_PREFIX)
    assert route_status == 200
    assert [page["relative_path"] for page in route_pages["pages"]] == [
        "concepts/review-workflow.md"
    ]


def test_v2_queue_route_candidates_favor_confident_same_document_routes(
    tmp_path: Path,
) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()
    write_routing_page(
        paths,
        relative_path="projects/northstar-transition-plan.md",
        title="Northstar Transition Plan",
        page_type="project",
    )
    write_routing_page(
        paths,
        relative_path="people/morgan.md",
        title="Morgan",
        page_type="person",
    )
    lint_wiki(paths)
    candidate = {
        **review_fact_payload("fact_morgan_unrouted"),
        "statement": (
            "Morgan recommended extending the transition by about 60 days "
            "to reach a contractual milestone."
        ),
        "page_hint": "concepts/extracted-facts.md",
        "metadata": {"document_id": "doc_northstar_transition"},
    }
    with connection(paths.sqlite_path) as conn:
        for index in range(3):
            conn.execute(
                """
                INSERT INTO facts(
                  id, statement, entity_key, page_hint, section_hint, source_ids,
                  observed_at, confidence, status, metadata, created_at,
                  routing_confidence, truth_confidence
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    f"fact_transition_sibling_{index}",
                    f"Northstar transition-plan detail {index}.",
                    "projects:northstar-transition-plan:summary",
                    "projects/northstar-transition-plan.md",
                    "Summary",
                    "[]",
                    "2026-07-10T00:00:00+00:00",
                    0.95,
                    "active",
                    dumps({"document_id": "doc_northstar_transition"}),
                    "2026-07-10T00:00:00+00:00",
                    0.95,
                    0.95,
                ),
            )
    insert_unrouted_question(
        paths,
        question_id="question_morgan_route",
        fact_id="fact_morgan_unrouted",
    )
    with connection(paths.sqlite_path) as conn:
        conn.execute(
            "UPDATE open_questions SET options = ? WHERE id = ?",
            (
                dumps([{"option_type": "candidate_fact", **candidate}]),
                "question_morgan_route",
            ),
        )

    with running_ui(paths) as (host, port, token):
        status, queue = request_json(
            host, port, token, "GET", "/api/queue?kind=unrouted"
        )

    assert status == 200
    routes = queue["items"][0]["route_candidates"]
    assert routes[0]["page_hint"] == "projects/northstar-transition-plan.md"
    assert routes[0]["document_coherence_count"] == 3
    assert routes[0]["document_coherence_share"] == 1.0


def test_v2_queue_limit_bounds_complete_card_enrichment(
    tmp_path: Path, monkeypatch
) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()
    for index in range(6):
        insert_unrouted_question(
            paths,
            question_id=f"question_unrouted_{index}",
            fact_id=f"fact_unrouted_{index}",
        )
    calls = 0

    def count_route_enrichment(
        *_args: object, **_kwargs: object
    ) -> list[dict[str, object]]:
        nonlocal calls
        calls += 1
        return []

    monkeypatch.setattr(ui_server, "route_candidates_for_fact", count_route_enrichment)

    with running_ui(paths) as (host, port, token):
        status, queue = request_json(
            host,
            port,
            token,
            "GET",
            "/api/queue?kind=unrouted&limit=1",
        )

    assert status == 200
    assert queue["total"] == 6
    assert len(queue["items"]) == 1
    assert calls == 1


def test_v2_queue_review_commands_use_numeric_keys() -> None:
    source = (
        Path(__file__).parents[1] / "src/pkm_brain/ui_static/views/queue.js"
    ).read_text(encoding="utf-8")

    assert "<kbd>1</kbd>keep existing" in source
    assert "<kbd>2</kbd>candidate wins" in source
    assert "<kbd>3</kbd>both true" in source
    assert "<kbd>4</kbd>supports existing" in source
    assert "<kbd>5</kbd>candidate current" in source
    assert "<kbd>6</kbd>unsure" in source
    assert 'key === "k") doDecision' not in source
    assert '1: "keep_existing"' in source
    assert '2: "candidate_wins"' in source
    assert '4: "supports_existing"' in source
    assert '5: "temporal_update"' in source
    assert "<kbd>1</kbd>approve" in source
    assert "<kbd>2</kbd>reject" in source
    assert "<kbd>3</kbd>skip" in source
    assert "<kbd>3</kbd>archive" in source
    assert 'data-decision="revert"' in source
    assert 'data-decision="mark_ok"' in source
    assert '"revert applied fact"' in source
    assert '"keep applied fact"' in source
    assert "Applied Change" in source
    assert "Representative Affected Facts" in source
    assert "No reversible applied change is available" in source
    assert '<input id="manual-route"' in source
    assert "<kbd>${keys.newPage}</kbd>route" in source
    assert 'ctx.api("/api/wiki/pages?routable=1")' in source
    assert 'item.comparison_mode === "alternatives"' in source
    assert 'data-alternative-id="${esc(factId)}"' in source
    assert 'doDecision(el, ctx, state, "select_facts"' in source
    assert "selected_fact_ids: selectedFactIds" in source
    assert 'data-review-state="blocked"' in source
    assert '[keys.reject]: "reject"' in source
    assert '[keys.skip]: "skip"' in source
    assert "<kbd>a</kbd>" not in source
    assert "<kbd>r</kbd>" not in source
    assert "<kbd>e</kbd>" not in source
    assert "<kbd>v</kbd>" not in source
    assert "<kbd>o</kbd>" not in source


def test_native_unrouted_route_buttons_register_displayed_shortcuts() -> None:
    root = Path(__file__).parents[1]
    source = (root / "app/Sources/Views/Queue/QueueView.swift").read_text(
        encoding="utf-8"
    )
    autocomplete = (
        root / "app/Sources/Views/Queue/RoutePathAutocompleteField.swift"
    ).read_text(encoding="utf-8")
    unrouted_card = source.split("private var unroutedCard", 1)[1].split(
        "private var memoryCard", 1
    )[0]

    assert 'routeFieldFocused ? "" : String(index + 1)' in unrouted_card
    assert "RoutePathAutocompleteField(" in unrouted_card
    assert "shortcutEnabled: !routeFieldFocused" in unrouted_card
    assert "client.routableWikiPages()" in autocomplete
    assert "RoutePathMatcher.suggestions" in autocomplete
    assert ".focused($fieldFocused)" in autocomplete
    assert ".onSubmit(onSubmit)" in autocomplete


def test_anomaly_controls_name_the_recorded_dispositions_clearly() -> None:
    root = Path(__file__).parents[1]
    native = (root / "app/Sources/Views/Queue/QueueView.swift").read_text(
        encoding="utf-8"
    )
    browser = (root / "src/pkm_brain/ui_static/views/queue.js").read_text(
        encoding="utf-8"
    )
    native_card = native.split("private var anomalyCard", 1)[1].split(
        "private var conflictCard", 1
    )[0]
    browser_card = browser.split("function anomalyCard", 1)[1].split(
        "function conflictCard", 1
    )[0]

    assert '"Confirm Quality Issue"' in native_card
    assert '"False Positive"' in native_card
    assert ">confirm quality issue</button>" in browser_card
    assert ">false positive</button>" in browser_card


def test_v2_queue_topology_surfaces_entity_names(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()
    with connection(paths.sqlite_path) as conn:
        conn.executemany(
            """
            INSERT INTO entities(id, name, entity_type, aliases, status, source_ids, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    "entity_primary",
                    "Alex",
                    "person",
                    "[]",
                    "active",
                    "[]",
                    "2026-07-09T10:00:00+00:00",
                ),
                (
                    "entity_duplicate",
                    "Alex Rivera",
                    "person",
                    "[]",
                    "active",
                    "[]",
                    "2026-07-09T10:00:00+00:00",
                ),
            ],
        )
    action = propose_action(
        paths,
        "entity_merge",
        action_payload={
            "canonical_entity_id": "entity_primary",
            "merged_entity_ids": ["entity_duplicate"],
            "reason": "entities share a normalized name or alias",
        },
        action_features={"reversible": True},
        proposed_by="test",
        risk_tier="low",
    )

    with running_ui(paths) as (host, port, token):
        status, queue = request_json(
            host,
            port,
            token,
            "GET",
            "/api/queue?kind=topology&limit=1",
        )

    assert status == 200
    item = queue["items"][0]
    assert item["id"] == action["id"]
    assert item["title"] == "Merge Alex Rivera into Alex"
    assert item["topology"]["target_label"] == "Alex Rivera into Alex"
    assert item["topology"]["entity_ids"] == ["entity_primary", "entity_duplicate"]
    assert item["topology"]["entity_labels"] == ["Alex", "Alex Rivera"]
    assert item["topology"]["entity_statuses"] == {
        "entity_primary": "active",
        "entity_duplicate": "active",
    }
    assert item["approvable"] is True


def test_v2_queue_excludes_entity_merge_after_target_drift(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()
    with connection(paths.sqlite_path) as conn:
        conn.executemany(
            """
            INSERT INTO entities(id, name, entity_type, aliases, status, source_ids, created_at)
            VALUES (?, ?, 'event', '[]', 'active', '[]', ?)
            """,
            [
                (
                    "entity_destination",
                    "Interview process",
                    "2026-07-12T10:00:00+00:00",
                ),
                ("entity_source", "PM interview process", "2026-07-12T10:00:00+00:00"),
                ("entity_other", "Interview loop", "2026-07-12T10:00:00+00:00"),
            ],
        )
    action = propose_action(
        paths,
        "entity_merge",
        action_payload={
            "canonical_entity_id": "entity_destination",
            "merged_entity_ids": ["entity_source"],
        },
        action_features={"confidence": 0.9, "reversible": True},
        proposed_by="test",
        risk_tier="low",
    )
    with connection(paths.sqlite_path) as conn:
        conn.execute(
            "UPDATE cos_actions SET status = 'needs_human' WHERE id = ?",
            (action["id"],),
        )
        conn.execute(
            "UPDATE entities SET status = 'merged', merged_into = 'entity_other' WHERE id = 'entity_source'"
        )
        conn.execute(
            """
            INSERT INTO open_questions(
              id, kind, fact_ids, question, options, status, context, action_id,
              recommended_action, risk_tier, created_at
            ) VALUES (?, 'policy_escalation', '[]', ?, '[]', 'needs_human', ?, ?, ?, 'low', ?)
            """,
            (
                "question_stale_entity_merge",
                "Entity merge requires review.",
                dumps({"action_id": action["id"]}),
                action["id"],
                dumps(
                    {
                        "action_type": "entity_merge",
                        "payload": {
                            "canonical_entity_id": "entity_destination",
                            "merged_entity_ids": ["entity_source"],
                        },
                    }
                ),
                "2026-07-12T10:01:00+00:00",
            ),
        )

    with running_ui(paths) as (host, port, token):
        status, queue = request_json(
            host, port, token, "GET", "/api/queue?kind=topology&state=all"
        )

    assert status == 200
    assert queue["total"] == 0
    assert queue["items"] == []
    assert queue["queue_summary"]["blocked_total"] == 0


def test_v2_queue_decision_applies_and_undoes_linked_action(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()
    fact = review_fact_payload("fact_v2_apply")
    action = propose_action(
        paths,
        "fact_upsert",
        action_payload={"fact": fact},
        action_features={"truth_mutation": True, "reversible": True},
        target_fact_ids=[str(fact["id"])],
        target_page_paths=[str(fact["page_hint"])],
        proposed_by="test",
        risk_tier="medium",
    )
    with connection(paths.sqlite_path) as conn:
        conn.execute(
            "UPDATE cos_actions SET status = 'needs_human' WHERE id = ?",
            (action["id"],),
        )
    insert_review_question_for_action(
        paths,
        question_id="question_v2_apply",
        action_id=action["id"],
        fact=fact,
    )

    with running_ui(paths) as (host, port, token):
        status, body = request_json(
            host,
            port,
            token,
            "POST",
            "/api/queue/question_v2_apply/decision",
            {"decision": "candidate_wins"},
        )
        undo_status, undo = request_json(
            host,
            port,
            token,
            "POST",
            "/api/queue/undo",
            {"undo_handle": body["undo_handle"]},
        )

    assert status == 200
    assert body["result"]["question"]["status"] == "answered"
    assert body["result"]["action"]["status"] == "applied"
    assert body["queue_summary"]["active_total"] == 0
    assert undo_status == 200
    assert undo["status"] == "undone"
    assert undo["queue_summary"]["active_total"] == 1
    assert undo["queue_summary"]["actionable_total"] == 1
    with connection(paths.sqlite_path) as conn:
        fact_count = conn.execute(
            "SELECT COUNT(*) FROM facts WHERE id = ?", (fact["id"],)
        ).fetchone()[0]
        question = conn.execute(
            "SELECT status, decided_by FROM open_questions WHERE id = ?",
            ("question_v2_apply",),
        ).fetchone()
        action_row = conn.execute(
            "SELECT status FROM cos_actions WHERE id = ?", (action["id"],)
        ).fetchone()
    assert fact_count == 0
    assert question["status"] == "needs_human"
    assert question["decided_by"] is None
    assert action_row["status"] == "needs_human"


def test_v2_queue_supports_existing_merges_provenance_only(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()
    apply_existing_review_fact(paths)
    fact = review_fact_payload("fact_supporting")
    action = propose_action(
        paths,
        "fact_upsert",
        action_payload={"fact": fact},
        action_features={"truth_mutation": True, "reversible": True},
        target_fact_ids=[str(fact["id"])],
        target_page_paths=[str(fact["page_hint"])],
        proposed_by="test",
        risk_tier="medium",
    )
    insert_review_question_for_action(
        paths,
        question_id="question_supports_existing",
        action_id=action["id"],
        fact=fact,
    )

    with running_ui(paths) as (host, port, token):
        status, body = request_json(
            host,
            port,
            token,
            "POST",
            "/api/queue/question_supports_existing/decision",
            {"decision": "supports_existing"},
        )

    assert status == 200
    assert body["result"]["question"]["answer"]["decision"] == "supports_existing"
    with connection(paths.sqlite_path) as conn:
        existing = conn.execute(
            "SELECT statement, source_ids, evidence_quote, metadata FROM facts WHERE id = 'fact_existing'"
        ).fetchone()
        old_action = conn.execute(
            "SELECT status FROM cos_actions WHERE id = ?", (action["id"],)
        ).fetchone()
    assert (
        existing["statement"] == "The old review workflow did not show enough evidence."
    )
    assert json.loads(existing["source_ids"]) == ["manual:existing", "manual:test"]
    assert (
        existing["evidence_quote"]
        == "The old review workflow did not show enough evidence."
    )
    assert (
        json.loads(existing["metadata"])["supporting_candidates"][0]["question_id"]
        == "question_supports_existing"
    )
    assert old_action["status"] == "rejected"


def test_v2_queue_temporal_update_supersedes_existing_fact(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()
    apply_existing_review_fact(paths)
    fact = review_fact_payload("fact_temporal_current")
    fact["statement"] = (
        "The review workflow now has enough evidence to make a decision."
    )
    action = propose_action(
        paths,
        "fact_upsert",
        action_payload={"fact": fact},
        action_features={"truth_mutation": True, "reversible": True},
        target_fact_ids=[str(fact["id"])],
        target_page_paths=[str(fact["page_hint"])],
        proposed_by="test",
        risk_tier="medium",
    )
    insert_review_question_for_action(
        paths,
        question_id="question_temporal_update",
        action_id=action["id"],
        fact=fact,
    )

    with running_ui(paths) as (host, port, token):
        status, body = request_json(
            host,
            port,
            token,
            "POST",
            "/api/queue/question_temporal_update/decision",
            {"decision": "temporal_update"},
        )

    assert status == 200
    assert body["result"]["question"]["answer"]["decision"] == "temporal_update"
    with connection(paths.sqlite_path) as conn:
        current = conn.execute(
            "SELECT status, supersedes_id, metadata FROM facts WHERE id = 'fact_temporal_current'"
        ).fetchone()
        existing = conn.execute(
            "SELECT status FROM facts WHERE id = 'fact_existing'"
        ).fetchone()
    assert current["status"] == "active"
    assert current["supersedes_id"] == "fact_existing"
    assert json.loads(current["metadata"])["temporal_update"][
        "superseded_fact_ids"
    ] == ["fact_existing"]
    assert existing["status"] == "superseded"


def test_m4_queue_acceptance_mixed_decisions_land_and_undo(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    svc = BrainService(paths)
    svc.init_workspace()
    write_concept_page(paths, generated=True)

    conflict_questions: list[str] = []
    conflict_actions: list[str] = []
    for index in range(5):
        fact = review_fact_payload(f"fact_m4_conflict_{index}")
        fact["statement"] = f"M4 conflict candidate {index} has enough evidence."
        fact["evidence_quote"] = f"M4 conflict candidate {index} has enough evidence."
        action = propose_action(
            paths,
            "fact_upsert",
            action_payload={"fact": fact},
            action_features={"truth_mutation": True, "reversible": True},
            target_fact_ids=[str(fact["id"])],
            target_page_paths=[str(fact["page_hint"])],
            proposed_by="m4-test",
            risk_tier="medium",
        )
        with connection(paths.sqlite_path) as conn:
            conn.execute(
                "UPDATE cos_actions SET status = 'needs_human' WHERE id = ?",
                (action["id"],),
            )
        question_id = f"question_m4_conflict_{index}"
        insert_review_question_for_action(
            paths,
            question_id=question_id,
            action_id=action["id"],
            fact=fact,
        )
        conflict_questions.append(question_id)
        conflict_actions.append(action["id"])

    unrouted_questions: list[str] = []
    for index in range(5):
        question_id = f"question_m4_unrouted_{index}"
        insert_unrouted_question(
            paths,
            question_id=question_id,
            fact_id=f"fact_m4_unrouted_{index}",
        )
        unrouted_questions.append(question_id)

    memory_ids = [
        svc.propose_memory(
            "FactMemory",
            "global",
            f"M4 proposed memory {index}.",
            ["manual:m4"],
            0.8 + (index / 100),
        )
        for index in range(5)
    ]

    topology_actions: list[str] = []
    for index in range(5):
        page_hint = f"concepts/m4-contract-{index}.md"
        action = propose_action(
            paths,
            "edit_contract",
            action_payload={
                "contract": {
                    "id": f"contract_m4_{index}",
                    "page_hint": page_hint,
                    "canonical_entity": f"concept:m4-{index}",
                    "page_scope": "acceptance",
                    "retrieval_purpose": "M4 queue acceptance",
                    "what_belongs_here": "Queue acceptance facts.",
                    "what_does_not_belong_here": "Unrelated material.",
                    "freshness_policy": "manual",
                    "related_pages": [],
                    "version": 1,
                    "status": "active",
                }
            },
            action_features={"reversible": True, "affected_fact_count": 0},
            target_page_paths=[page_hint],
            proposed_by="m4-test",
            risk_tier="low",
        )
        topology_actions.append(action["id"])

    with running_ui(paths) as (host, port, token):
        queue_status, queue = request_json(
            host, port, token, "GET", "/api/queue?limit=25"
        )
        assert queue_status == 200
        assert queue["total"] == 20
        assert queue["counts"]["by_kind"]["conflicts"] == 5
        assert queue["counts"]["by_kind"]["unrouted"] == 5
        assert queue["counts"]["by_kind"]["memories"] == 5
        assert queue["counts"]["by_kind"]["topology"] == 5
        first_conflict = next(
            item for item in queue["items"] if item["id"] == conflict_questions[0]
        )
        assert first_conflict["candidate"]["evidence_quote"]
        assert first_conflict["counterparts"][0]["evidence_quote"]

        status, body = request_json(
            host,
            port,
            token,
            "POST",
            f"/api/queue/{conflict_questions[0]}/decision",
            {"decision": "candidate_wins"},
        )
        assert status == 200
        undo_status, undo = request_json(
            host,
            port,
            token,
            "POST",
            "/api/queue/undo",
            {"undo_handle": body["undo_handle"]},
        )
        assert undo_status == 200
        assert undo["status"] == "undone"
        status, _body = request_json(
            host,
            port,
            token,
            "POST",
            f"/api/queue/{conflict_questions[0]}/decision",
            {"decision": "reject"},
        )
        assert status == 200

        for question_id in conflict_questions[1:]:
            status, _body = request_json(
                host,
                port,
                token,
                "POST",
                f"/api/queue/{question_id}/decision",
                {"decision": "candidate_wins"},
            )
            assert status == 200

        for index, question_id in enumerate(unrouted_questions):
            status, _body = request_json(
                host,
                port,
                token,
                "POST",
                f"/api/queue/{question_id}/decision",
                {"decision": "route", "page_hint": f"concepts/m4-routed-{index}.md"},
            )
            assert status == 200

        for memory_id, decision in zip(
            memory_ids,
            ["approve", "reject", "archive", "approve", "reject"],
            strict=True,
        ):
            status, _body = request_json(
                host,
                port,
                token,
                "POST",
                f"/api/queue/{memory_id}/decision",
                {"decision": decision},
            )
            assert status == 200

        for action_id, decision in zip(
            topology_actions,
            ["approve", "reject", "approve", "reject", "approve"],
            strict=True,
        ):
            status, _body = request_json(
                host,
                port,
                token,
                "POST",
                f"/api/queue/{action_id}/decision",
                {"decision": decision},
            )
            assert status == 200

        final_status, final_queue = request_json(host, port, token, "GET", "/api/queue")
        assert final_status == 200
        assert final_queue["total"] == 0

    with connection(paths.sqlite_path) as conn:
        question_rows = {
            row["id"]: row
            for row in conn.execute(
                "SELECT id, status, decided_by FROM open_questions ORDER BY id"
            )
        }
        memory_rows = {
            row["id"]: row["status"]
            for row in conn.execute(
                f"SELECT id, status FROM memories WHERE id IN ({','.join('?' for _ in memory_ids)})",
                memory_ids,
            )
        }
        action_rows = {
            row["id"]: row["status"]
            for row in conn.execute(
                f"SELECT id, status FROM cos_actions WHERE id IN ({','.join('?' for _ in [*conflict_actions, *topology_actions])})",
                [*conflict_actions, *topology_actions],
            )
        }
        routed_fact_count = conn.execute(
            """
            SELECT COUNT(*)
            FROM facts
            WHERE id LIKE 'fact_m4_unrouted_%'
              AND page_hint LIKE 'concepts/m4-routed-%.md'
            """
        ).fetchone()[0]
        contract_count = conn.execute(
            "SELECT COUNT(*) FROM page_contracts WHERE id LIKE 'contract_m4_%'"
        ).fetchone()[0]

    assert question_rows[conflict_questions[0]]["status"] == "dismissed"
    assert question_rows[conflict_questions[0]]["decided_by"] == "human"
    for question_id in [*conflict_questions[1:], *unrouted_questions]:
        assert question_rows[question_id]["status"] == "answered"
        assert question_rows[question_id]["decided_by"] == "human"
    assert action_rows[conflict_actions[0]] == "rejected"
    for action_id in conflict_actions[1:]:
        assert action_rows[action_id] == "applied"
    assert [memory_rows[memory_id] for memory_id in memory_ids] == [
        "active",
        "rejected",
        "archived",
        "active",
        "rejected",
    ]
    assert [action_rows[action_id] for action_id in topology_actions] == [
        "applied",
        "rejected",
        "applied",
        "rejected",
        "applied",
    ]
    assert routed_fact_count == 5
    assert contract_count == 3


def test_v2_entities_index_and_detail_surface_identity_layer(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()
    with connection(paths.sqlite_path) as conn:
        conn.execute(
            """
            INSERT INTO entities(id, name, entity_type, aliases, status, source_ids, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "entity_alpha",
                "AlphaPay",
                "product",
                json.dumps(["Alpha Pay"]),
                "active",
                "[]",
                "2026-07-06T10:00:00+00:00",
            ),
        )
        conn.execute(
            """
            INSERT INTO facts(
              id, statement, entity_key, entity_id, page_hint, section_hint,
              source_ids, observed_at, confidence, status, metadata, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "fact_alpha",
                "AlphaPay has a browser-visible entity page.",
                "products:alphapay:summary",
                "entity_alpha",
                "products/alphapay.md",
                "Summary",
                "[]",
                "2026-07-06T10:01:00+00:00",
                0.9,
                "active",
                "{}",
                "2026-07-06T10:01:00+00:00",
            ),
        )
        conn.execute(
            """
            INSERT INTO fact_entities(id, fact_id, entity_id, is_primary, mention_text, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                "fe_alpha",
                "fact_alpha",
                "entity_alpha",
                1,
                "AlphaPay",
                "2026-07-06T10:01:00+00:00",
            ),
        )

    with running_ui(paths) as (host, port, token):
        index_status, index = request_json(host, port, token, "GET", "/api/entities")
        detail_status, detail = request_json(
            host, port, token, "GET", "/api/entities/entity_alpha"
        )

    assert index_status == 200
    assert index["entities"][0]["name"] == "AlphaPay"
    assert index["entities"][0]["fact_count"] == 1
    assert detail_status == 200
    assert detail["entity"]["aliases"] == ["Alpha Pay"]
    assert detail["facts_by_page"][0]["page_hint"] == "products/alphapay.md"
    assert (
        detail["facts_by_page"][0]["facts"][0]["statement"]
        == "AlphaPay has a browser-visible entity page."
    )


def test_entities_and_queue_sort_by_distinct_retrieval_popularity(
    tmp_path: Path,
) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()
    with connection(paths.sqlite_path) as conn:
        for suffix, retrieval_count in (("popular", 3), ("quiet", 1)):
            entity_id = f"entity_{suffix}"
            fact_id = f"fact_{suffix}"
            conn.execute(
                """
                INSERT INTO entities(id, name, entity_type, aliases, status, source_ids, created_at)
                VALUES (?, ?, 'concept', '[]', 'active', '[]', ?)
                """,
                (entity_id, suffix.title(), "2026-07-01T00:00:00+00:00"),
            )
            conn.execute(
                """
                INSERT INTO facts(
                  id, statement, entity_key, entity_id, page_hint, section_hint,
                  source_ids, observed_at, confidence, status, metadata, created_at
                ) VALUES (?, ?, ?, ?, ?, 'Summary', '[]', ?, 0.9, 'active', '{}', ?)
                """,
                (
                    fact_id,
                    f"{suffix.title()} retrieval fact.",
                    f"concepts:{suffix}:summary",
                    entity_id,
                    f"concepts/{suffix}.md",
                    "2026-07-01T00:00:00+00:00",
                    "2026-07-01T00:00:00+00:00",
                ),
            )
            conn.execute(
                """
                INSERT INTO fact_entities(id, fact_id, entity_id, is_primary, mention_text, created_at)
                VALUES (?, ?, ?, 1, ?, ?)
                """,
                (
                    f"fe_{suffix}",
                    fact_id,
                    entity_id,
                    suffix.title(),
                    "2026-07-01T00:00:00+00:00",
                ),
            )
            conn.execute(
                """
                INSERT INTO open_questions(
                  id, kind, entity_key, page_hint, fact_ids, question, options,
                  status, context, risk_tier, created_at
                ) VALUES (?, 'conflict', ?, ?, ?, ?, '[]', 'needs_human', '{}', 'medium', ?)
                """,
                (
                    f"question_{suffix}",
                    f"concepts:{suffix}:summary",
                    f"concepts/{suffix}.md",
                    dumps([fact_id]),
                    f"Review {suffix} fact.",
                    "2026-07-02T00:00:00+00:00",
                ),
            )
            for index in range(retrieval_count):
                conn.execute(
                    """
                    INSERT INTO context_lineage_events(
                      id, target_type, target_id, event_type, retrieval_event_id,
                      query, weight, metadata, created_at
                    ) VALUES (?, 'fact', ?, 'exposed', ?, ?, 0.0, '{}', ?)
                    """,
                    (
                        f"lineage_{suffix}_{index}",
                        fact_id,
                        f"retrieval_{suffix}_{index}",
                        f"query {index}",
                        f"2026-07-0{index + 2}T00:00:00+00:00",
                    ),
                )
        for index in range(5):
            conn.execute(
                """
                INSERT INTO open_questions(
                  id, kind, question, options, status, context, risk_tier, created_at
                ) VALUES (?, 'conflict', ?, '[]', 'needs_human', '{}', 'medium', ?)
                """,
                (
                    f"question_decoy_{index}",
                    f"Review newer quiet item {index}.",
                    f"2026-07-03T00:00:0{index}+00:00",
                ),
            )

    with running_ui(paths) as (host, port, token):
        entity_status, entity_index = request_json(
            host, port, token, "GET", "/api/entities?sort=retrieval"
        )
        detail_status, detail = request_json(
            host, port, token, "GET", "/api/entities/entity_popular"
        )
        queue_status, queue = request_json(
            host,
            port,
            token,
            "GET",
            "/api/queue?state=all&sort=retrieval&limit=1",
        )

    assert entity_status == 200
    assert [row["id"] for row in entity_index["entities"]] == [
        "entity_popular",
        "entity_quiet",
    ]
    assert entity_index["entities"][0]["retrieval_count"] == 3
    assert detail_status == 200
    assert detail["entity"]["retrieval_count"] == 3
    assert detail["facts_by_page"][0]["facts"][0]["retrieval_count"] == 3
    assert queue_status == 200
    assert queue["sort"] == "retrieval"
    assert queue["items"][0]["id"] == "question_popular"
    assert queue["items"][0]["popularity"]["retrieval_count"] == 3
    assert queue["next_cursor"] == 1


def test_curation_settings_promote_future_only_policy_profiles(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()
    paths.config_file.write_text(
        "embedding:\n  provider: hash\n",
        encoding="utf-8",
    )
    action = propose_action(
        paths,
        "fact_merge",
        action_payload={"canonical_fact_id": "fact_a", "merged_fact_ids": ["fact_b"]},
        action_features={"confidence": 0.99},
        target_fact_ids=["fact_a", "fact_b"],
        confidence=0.99,
        risk_tier="medium",
    )
    with connection(paths.sqlite_path) as conn:
        before_policy = conn.execute("SELECT MAX(version) FROM cos_policy").fetchone()[
            0
        ]

    with running_ui(paths) as (host, port, token):
        default_status, default_settings = request_json(
            host, port, token, "GET", "/api/settings/curation"
        )
        strict_status, strict = request_json(
            host,
            port,
            token,
            "PUT",
            "/api/settings/curation",
            {"strictness": "strict"},
        )

    assert default_status == 200
    assert default_settings["strictness"] == "balanced"
    assert default_settings["merge_aggressiveness"] == 0.5
    assert default_settings["split_aggressiveness"] == 0.5
    assert default_settings["topology_review_threshold"] == 8
    assert default_settings["topology_applies_to"] == "future_gardener_runs_only"
    assert (
        "unconfirmed_topology_above_review_threshold"
        in default_settings["hard_review_boundaries"]
    )
    assert "failed_topology_judgment" in default_settings["hard_review_boundaries"]
    assert default_settings["updated_at"] is not None
    assert "." not in default_settings["updated_at"]
    assert default_settings["configured"] is False
    assert strict_status == 200
    assert strict["strictness"] == "strict"
    assert strict["minimum_auto_confidence"] == 0.95
    assert strict["applies_to"] == "future_actions_only"
    assert strict["existing_queue_unchanged"] is True
    assert strict["policy_version"] == before_policy + 1
    assert strict["updated_at"] is not None
    assert "provider: hash" in paths.config_file.read_text(encoding="utf-8")
    with connection(paths.sqlite_path) as conn:
        assert (
            conn.execute(
                "SELECT status FROM cos_actions WHERE id = ?", (action["id"],)
            ).fetchone()[0]
            == "proposed"
        )
        strict_medium = evaluate_policy(
            conn,
            "fact_merge",
            {"risk_tier": "medium", "confidence": 0.99},
        )
        strict_exact = evaluate_policy(
            conn,
            "fact_upsert",
            {
                "risk_tier": "low",
                "confidence": 0.96,
                "fact_upsert_resolution": "exact_duplicate_source_union",
                "quote_backed": True,
                "fallback_route": False,
            },
        )
        hard_boundary = evaluate_policy(
            conn,
            "fact_upsert",
            {"risk_tier": "high", "confidence": 1.0, "truth_contradiction": True},
        )
        strict_duplicate_merge = evaluate_policy(
            conn,
            "page_merge",
            {
                "candidate_signal": (
                    "near-duplicate page hints with overlapping fact evidence"
                ),
                "duplicate_page_merge_signal": True,
                "gardener_confirmed": True,
                "contract_compatible": True,
                "cross_entity_merge": False,
                "cross_type_merge": False,
                "type_mismatch": False,
                "reversible": True,
                "truth_mutation": False,
                "confidence": 0.99,
                "risk_tier": "high",
                "affected_fact_count": 40,
                "large_topology": True,
            },
        )
    assert strict_medium.autonomy_level == "L3"
    assert strict_exact.autonomy_level == "L1"
    assert hard_boundary.autonomy_level == "L3"
    assert strict_duplicate_merge.autonomy_level == "L3"

    with running_ui(paths) as (host, port, token):
        topology_status, topology = request_json(
            host,
            port,
            token,
            "PUT",
            "/api/settings/curation",
            {"merge_aggressiveness": 0.8, "split_aggressiveness": 0.2},
        )
        invalid_status, invalid = request_json(
            host,
            port,
            token,
            "PUT",
            "/api/settings/curation",
            {"split_aggressiveness": 1.2},
        )
        threshold_status, threshold = request_json(
            host,
            port,
            token,
            "PUT",
            "/api/settings/curation",
            {"topology_review_threshold": 32},
        )
        invalid_threshold_status, invalid_threshold = request_json(
            host,
            port,
            token,
            "PUT",
            "/api/settings/curation",
            {"topology_review_threshold": 32.5},
        )
    assert topology_status == 200
    assert topology["strictness"] == "strict"
    assert topology["merge_aggressiveness"] == 0.8
    assert topology["split_aggressiveness"] == 0.2
    assert topology["policy_version"] == strict["policy_version"]
    assert topology["updated_at"] >= strict["updated_at"]
    assert invalid_status == 400
    assert "split_aggressiveness must be between 0 and 1" in invalid["error"]
    assert threshold_status == 200
    assert threshold["topology_review_threshold"] == 32
    assert threshold["policy_version"] == strict["policy_version"] + 1
    assert invalid_threshold_status == 400
    assert "must be an integer" in invalid_threshold["error"]
    config_text = paths.config_file.read_text(encoding="utf-8")
    assert "merge_aggressiveness: 0.8" in config_text
    assert "split_aggressiveness: 0.2" in config_text
    assert "topology_review_threshold: 32" in config_text
    with connection(paths.sqlite_path) as conn:
        below_topology_threshold = evaluate_policy(
            conn,
            "page_split",
            {
                "risk_tier": "medium",
                "confidence": 0.99,
                "affected_fact_count": 20,
                "large_topology": False,
            },
        )
        above_topology_threshold = evaluate_policy(
            conn,
            "page_split",
            {
                "risk_tier": "medium",
                "confidence": 0.99,
                "affected_fact_count": 33,
                "large_topology": False,
            },
        )
    assert below_topology_threshold.autonomy_level == "L2"
    assert above_topology_threshold.autonomy_level == "L3"

    with running_ui(paths) as (host, port, token):
        lenient_status, lenient = request_json(
            host,
            port,
            token,
            "PUT",
            "/api/settings/curation",
            {"strictness": "lenient"},
        )
    assert lenient_status == 200
    assert lenient["minimum_auto_confidence"] == 0.6
    assert lenient["merge_aggressiveness"] == 0.8
    assert lenient["split_aggressiveness"] == 0.2
    with connection(paths.sqlite_path) as conn:
        lenient_medium = evaluate_policy(
            conn,
            "fact_merge",
            {"risk_tier": "medium", "confidence": 0.65},
        )
        below_floor = evaluate_policy(
            conn,
            "fact_merge",
            {"risk_tier": "medium", "confidence": 0.59},
        )
    assert lenient_medium.autonomy_level == "L2"
    assert below_floor.autonomy_level == "L3"


def test_cos_review_apply_action_endpoint_applies_linked_action(
    tmp_path: Path,
) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()
    fact = review_fact_payload()
    action = propose_action(
        paths,
        "fact_upsert",
        action_payload={"fact": fact},
        action_features={"truth_mutation": True, "reversible": True},
        target_fact_ids=[str(fact["id"])],
        target_page_paths=[str(fact["page_hint"])],
        proposed_by="test",
        risk_tier="medium",
    )
    with connection(paths.sqlite_path) as conn:
        conn.execute(
            "UPDATE cos_actions SET status = 'needs_human' WHERE id = ?",
            (action["id"],),
        )
    insert_review_question_for_action(
        paths,
        question_id="question_review_apply",
        action_id=action["id"],
        fact=fact,
    )

    with running_ui(paths) as (host, port, token):
        status, body = request_json(
            host,
            port,
            token,
            "POST",
            "/api/cos/questions/question_review_apply/apply-action",
            {"note": "looks supported"},
        )

    assert status == 200
    assert body["question"]["status"] == "answered"
    assert body["question"]["answer"]["decision"] == "apply_action"
    assert body["action"]["status"] == "applied"
    with connection(paths.sqlite_path) as conn:
        fact_row = conn.execute(
            "SELECT statement, status FROM facts WHERE id = ?", (fact["id"],)
        ).fetchone()
        question_row = conn.execute(
            "SELECT status, decided_by FROM open_questions WHERE id = ?",
            ("question_review_apply",),
        ).fetchone()
    assert fact_row["statement"] == fact["statement"]
    assert fact_row["status"] == "active"
    assert question_row["status"] == "answered"
    assert question_row["decided_by"] == "human"


def test_cos_review_dismiss_endpoint_rejects_linked_action(
    tmp_path: Path,
) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()
    fact = review_fact_payload("fact_rejected")
    action = propose_action(
        paths,
        "fact_upsert",
        action_payload={"fact": fact},
        action_features={"truth_mutation": True, "reversible": True},
        target_fact_ids=[str(fact["id"])],
        target_page_paths=[str(fact["page_hint"])],
        proposed_by="test",
        risk_tier="medium",
    )
    insert_review_question_for_action(
        paths,
        question_id="question_review_reject",
        action_id=action["id"],
        fact=fact,
    )

    with running_ui(paths) as (host, port, token):
        status, body = request_json(
            host,
            port,
            token,
            "POST",
            "/api/cos/questions/question_review_reject/dismiss",
            {"reason": "not supported by quote"},
        )

    assert status == 200
    assert body["question"]["status"] == "dismissed"
    assert body["question"]["answer"]["decision"] == "dismiss"
    assert body["action"]["status"] == "rejected"
    assert (
        body["action"]["evidence_json"]["human_review"]["reason"]
        == "not supported by quote"
    )
    with connection(paths.sqlite_path) as conn:
        fact_count = conn.execute(
            "SELECT COUNT(*) FROM facts WHERE id = ?", (fact["id"],)
        ).fetchone()[0]
        question_row = conn.execute(
            "SELECT status, decided_by FROM open_questions WHERE id = ?",
            ("question_review_reject",),
        ).fetchone()
    assert fact_count == 0
    assert question_row["status"] == "dismissed"
    assert question_row["decided_by"] == "human"


def test_legacy_wiki_proposal_endpoints_are_retired(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()

    with running_ui(paths) as (host, port, token):
        checks = [
            request_json(host, port, token, "GET", "/api/wiki/proposals"),
            request_json(host, port, token, "GET", "/api/wiki/proposals/batch_old"),
            request_json(host, port, token, "GET", "/api/wiki/proposal-packets"),
            request_json(host, port, token, "GET", "/api/review-queue"),
            request_json(host, port, token, "POST", "/api/wiki/proposals", {}),
            request_json(
                host, port, token, "POST", "/api/wiki/proposal-packets/facts", {}
            ),
            request_json(host, port, token, "POST", "/api/wiki/facts/migrate-wiki", {}),
        ]

    assert [status for status, _body in checks] == [404, 404, 404, 404, 404, 404, 404]


def test_memory_endpoint_lists_status_filtered_memories(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    svc = BrainService(paths)
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
    svc = BrainService(paths)
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
    svc = BrainService(paths)
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
    svc = BrainService(paths)
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
    svc = BrainService(paths)
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


def test_atlas_cloud_source_backed_alternatives_merge_as_same_fact() -> None:
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
        "The Atlas Cloud interview prompt centered on designing capabilities for a FinOps administrator to "
        "share dashboards and related analysis with immediate teammates and leaders across the organization. "
        "The core problem is not merely distributing dashboards. It is enabling the right people to collaborate "
        "on or consume financial and cost analysis based on their role, team context, and permissions. Primary "
        "users discussed: FinOps administrator, internal FinOps teammates, and cross-functional team leads who "
        "need visibility into cost or financial reporting. A useful access model distinguishes between groups "
        "inherited from an identity source and custom groups created by a FinOps administrator."
    )
    right = (
        "The May 29, 2026 Atlas Cloud interview focused on designing capabilities for a FinOps administrator to "
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


def test_wiki_fact_reconcile_dismisses_stale_duplicate_atlas_cloud_question(
    tmp_path: Path,
) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    svc = BrainService(paths)
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
        "The Atlas Cloud interview prompt centered on designing capabilities for a FinOps administrator to "
        "share dashboards and related analysis with immediate teammates and leaders across the organization. "
        "The core problem is not merely distributing dashboards. It is enabling role-aware collaboration and "
        "consumption of financial and cost analysis based on team context and permissions."
    )
    right = (
        "The May 29, 2026 Atlas Cloud interview focused on designing capabilities for a FinOps administrator to "
        "share dashboards and related analysis with both their immediate team and leaders in other parts of the "
        "organization. The stronger framing was role-aware collaboration and consumption of cost analysis."
    )
    fact_ids = ["fact_atlas_cloud_a", "fact_atlas_cloud_b"]
    question_id = "question_atlas_cloud"
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
                    "concepts:atlas_cloud-dashboard-sharing-interview:summary",
                    "concepts/test-concept.md",
                    "Summary",
                    dumps(source_ids),
                    observed_at,
                    0.84,
                    "conflicted",
                    "factconflict_atlas_cloud",
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
                "concepts:atlas_cloud-dashboard-sharing-interview:summary",
                "concepts/test-concept.md",
                dumps(fact_ids),
                "What is currently true for Atlas Cloud dashboard sharing?",
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
                dumps({"conflict_group_id": "factconflict_atlas_cloud"}),
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


def test_chief_of_staff_page_review_correction_and_revert_endpoint(
    tmp_path: Path,
) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    svc = BrainService(paths)
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
    reviewed_fact = next(
        fact for fact in review["facts"] if fact["id"] == "fact_ui_original"
    )
    assert reviewed_fact["source_date"] == "2026-05-25T00:00:00+00:00"
    assert reviewed_fact["source_date_basis"] == "source_created_at"
    assert reviewed_fact["source_documents"][0]["title"] == "Source Evidence"
    assert correction_status == 200
    assert correction["fact"]["confirmed_by_user"] is True
    assert correction["action"]["action_type"] == "fact_upsert"
    assert correction["action"]["status"] == "applied"
    assert "corrected fact" in correction["review"]["current_markdown"]
    assert revert_status == 200
    assert "original fact" in reverted["review"]["current_markdown"]


def test_cos_control_plane_endpoints_require_auth_and_return_state(
    tmp_path: Path,
) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()
    auto_action = decide_action(
        paths,
        propose_action(
            paths,
            "canonicalize_page",
            action_payload={"page_hint": "concepts/test.md"},
            action_features={
                "deterministic": True,
                "risk_score": 0.01,
                "risk_tier": "low",
            },
            target_page_paths=["concepts/test.md"],
        )["id"],
    )
    record_action_audit(
        paths,
        auto_action["id"],
        "sampled_bad",
        metadata={"reason": "ui test failure"},
    )
    human_action = decide_action(
        paths,
        propose_action(
            paths,
            "fact_upsert",
            action_payload={
                "fact": {
                    "id": "fact_ui_policy",
                    "statement": "UI policy gated fact.",
                    "entity_key": "concepts:test:summary",
                    "page_hint": "concepts/test.md",
                    "confidence": 0.6,
                }
            },
            action_features={"truth_mutation": False, "risk_score": 0.4},
            target_page_paths=["concepts/test.md"],
        )["id"],
    )
    with running_ui(paths) as (host, port, token):
        unauthorized, _ = request_json(host, port, None, "GET", "/api/cos/policy")
        policy_status, policy = request_json(
            host, port, token, "GET", "/api/cos/policy"
        )
        actions_status, actions = request_json(
            host, port, token, "GET", "/api/cos/actions"
        )
        review_status, review = request_json(
            host, port, token, "GET", "/api/cos/review"
        )
        contracts_status, contracts = request_json(
            host, port, token, "GET", "/api/cos/contracts"
        )
        audit_status, audit = request_json(host, port, token, "GET", "/api/cos/audit")

    assert unauthorized == 401
    assert policy_status == 200
    assert policy["version"] == 1
    assert policy["rules"]
    assert actions_status == 200
    assert {action["id"] for action in actions["actions"]} == {
        auto_action["id"],
        human_action["id"],
    }
    assert review_status == 200
    assert review["policy_version"] == 1
    assert review["counts"]["residue"] == 1
    assert review["residue"][0]["action_id"] == human_action["id"]
    assert review["recent_auto_applied"][0]["id"] == auto_action["id"]
    assert review["audit_failures"][0]["id"] == auto_action["id"]
    assert contracts_status == 200
    assert contracts["contracts"] == []
    assert audit_status == 200
    assert audit["status"] == "ok"
    assert audit["mode"] == "configured"
    assert audit["counts"]["sampled_bad"] == 1
