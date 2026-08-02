from __future__ import annotations

import http.client
import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import pytest

from pkm_brain import (
    question_resolution,
    queue_undo_transaction,
    ui_server,
    wiki_facts,
)
from pkm_brain.contracts import insert_contract_direct
from pkm_brain.cos_actions import (
    apply_action,
    decide_action,
    load_action,
    propose_action,
    record_action_audit,
    target_state_hash,
)
from pkm_brain.cos_policy import evaluate_policy
from pkm_brain.db import connection, dumps
from pkm_brain.paths import BrainPaths
from pkm_brain.review_resolution import (
    ReviewResolutionConflict,
    active_resolution_for_action,
    record_review_resolution,
)
from pkm_brain.review_undo import ReviewUndoError, seal_undo_handle
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


def insert_legacy_answer_question(
    paths: BrainPaths, *, question_id: str
) -> tuple[dict[str, object], dict[str, object]]:
    left = existing_review_fact_payload(f"fact_{question_id}_left")
    right = review_fact_payload(f"fact_{question_id}_right")
    right["statement"] = "The current review workflow shows complete evidence."
    right["evidence_quote"] = right["statement"]
    for fact in (left, right):
        apply_action(
            paths,
            propose_action(
                paths,
                "fact_upsert",
                action_payload={"fact": fact},
                action_features={"truth_mutation": True, "reversible": True},
                target_fact_ids=[str(fact["id"])],
                target_page_paths=[str(fact["page_hint"])],
                proposed_by="test_legacy_answer",
                risk_tier="low",
            )["id"],
        )
    fact_ids = [str(left["id"]), str(right["id"])]
    with connection(paths.sqlite_path) as conn:
        conn.execute(
            """
            UPDATE facts
            SET status = 'conflicted', conflict_group_id = ?
            WHERE id IN (?, ?)
            """,
            (f"conflict_{question_id}", *fact_ids),
        )
        conn.execute(
            """
            INSERT INTO open_questions(
              id, kind, entity_key, page_hint, fact_ids, question, options,
              status, context, risk_tier, created_at
            ) VALUES (?, 'conflict', ?, ?, ?, ?, ?, 'open', ?, 'medium', ?)
            """,
            (
                question_id,
                right["entity_key"],
                right["page_hint"],
                dumps(fact_ids),
                "Which review-workflow fact should be retained?",
                dumps(
                    [
                        {"fact_id": fact["id"], "statement": fact["statement"]}
                        for fact in (left, right)
                    ]
                ),
                dumps({"conflict_group_id": f"conflict_{question_id}"}),
                "2026-07-06T10:01:00+00:00",
            ),
        )
    return left, right


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
        initial_status, initial = request_json(host, port, token, "GET", "/api/queue")
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

        active_status, active = request_json(host, port, token, "GET", "/api/queue")
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
            existing["id"]: ("retracted", 0),
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
        after_status, after = request_json(
            host, port, token, "GET", "/api/queue?kind=audit"
        )
        undo_status, _undo = request_json(
            host,
            port,
            token,
            "POST",
            "/api/queue/undo",
            {"undo_handle": decision["undo_handle"]},
        )
        reopened_status, reopened = request_json(
            host, port, token, "GET", "/api/queue?kind=audit"
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
    assert decision["result"]["confirmation"]["fact"]["confirmed_by_user"] is True
    assert after_status == 200
    assert after["total"] == 0
    assert undo_status == 200
    assert reopened_status == 200
    assert reopened["total"] == 1
    with connection(paths.sqlite_path) as conn:
        fact_row = conn.execute(
            "SELECT confirmed_by_user FROM facts WHERE id = ?", (fact["id"],)
        ).fetchone()
        resolution = conn.execute(
            """
            SELECT disposition, revoked_at
            FROM review_resolutions
            WHERE source_item_kind = 'audit' AND source_item_id = ?
            """,
            (action["id"],),
        ).fetchone()
    assert bool(fact_row["confirmed_by_user"]) is False
    assert resolution["disposition"] == "keep"
    assert resolution["revoked_at"] is not None


def test_v2_queue_reverts_exact_audit_siblings_as_one_undoable_issue(
    tmp_path: Path,
) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()
    fact = review_fact_payload("fact_duplicate_audit")
    actions = []
    for _ in range(2):
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
        record_action_audit(
            paths,
            action["id"],
            "sampled_bad",
            metadata={"rationale": "The same semantic issue was found twice."},
        )
        actions.append(action)

    with running_ui(paths) as (host, port, token):
        queue_status, queue = request_json(
            host, port, token, "GET", "/api/queue?kind=audit"
        )
        representative_id = str(queue["items"][0]["id"])
        decision_status, decision = request_json(
            host,
            port,
            token,
            "POST",
            f"/api/queue/{representative_id}/decision",
            {"decision": "revert"},
        )
        after_status, after = request_json(
            host, port, token, "GET", "/api/queue?kind=audit"
        )
        undo_status, _undo = request_json(
            host,
            port,
            token,
            "POST",
            "/api/queue/undo",
            {"undo_handle": decision["undo_handle"]},
        )
        reopened_status, reopened = request_json(
            host, port, token, "GET", "/api/queue?kind=audit"
        )

    assert queue_status == 200
    assert queue["total"] == 1
    assert queue["items"][0]["audit"]["related_action_count"] == 2
    assert decision_status == 200
    assert decision["undo_handle"]["kind"] == "audit_batch_remediation"
    assert len(decision["result"]["related_results"]) == 2
    assert after_status == 200
    assert after["total"] == 0
    assert undo_status == 200
    assert reopened_status == 200
    assert reopened["total"] == 1
    with connection(paths.sqlite_path) as conn:
        action_rows = list(
            conn.execute(
                "SELECT status, audit_status FROM cos_actions WHERE id IN (?, ?)",
                [action["id"] for action in actions],
            )
        )
        fact_row = conn.execute(
            "SELECT status FROM facts WHERE id = ?", (fact["id"],)
        ).fetchone()
    assert [(row["status"], row["audit_status"]) for row in action_rows] == [
        ("applied", "sampled_bad"),
        ("applied", "sampled_bad"),
    ]
    assert fact_row["status"] == "active"


def test_direct_fact_confirmation_supersedes_exact_rejection(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()
    fact = review_fact_payload("fact_confirm_after_reject")
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
    with connection(paths.sqlite_path) as conn:
        rejected, created = record_review_resolution(
            conn,
            action,
            disposition="reject",
            source_item_kind="audit",
            source_item_id=action["id"],
        )
    assert created is True

    with running_ui(paths) as (host, port, token):
        confirm_status, confirmed = request_json(
            host,
            port,
            token,
            "POST",
            f"/api/wiki/facts/{fact['id']}/confirm",
        )

    assert confirm_status == 200
    assert confirmed["fact"]["confirmed_by_user"] is True
    assert confirmed["action"]["status"] == "applied"
    with connection(paths.sqlite_path) as conn:
        rows = list(
            conn.execute(
                """
                SELECT id, disposition, revoked_at
                FROM review_resolutions
                WHERE family_key = ?
                ORDER BY resolved_at, id
                """,
                (rejected["family_key"],),
            )
        )
    assert {row["disposition"]: row["revoked_at"] is None for row in rows} == {
        "reject": False,
        "keep": True,
    }


def test_direct_fact_confirmation_rolls_back_with_resolution_transaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()
    fact = review_fact_payload("fact_confirm_resolution_failure")
    apply_action(
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
    write_resolution = ui_server.record_fact_confirmation_resolution

    def fail_after_resolution(*args: object, **kwargs: object) -> None:
        write_resolution(*args, **kwargs)
        raise RuntimeError("injected confirmation resolution failure")

    monkeypatch.setattr(
        ui_server, "record_fact_confirmation_resolution", fail_after_resolution
    )
    with pytest.raises(RuntimeError, match="confirmation resolution failure"):
        ui_server.ui_confirm_fact(paths, fact["id"])

    with connection(paths.sqlite_path) as conn:
        restored_fact = conn.execute(
            "SELECT confirmed_by_user FROM facts WHERE id = ?", (fact["id"],)
        ).fetchone()
        confirmation_action = conn.execute(
            """
            SELECT status FROM cos_actions
            WHERE proposed_by = 'ui_fact_confirm'
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """
        ).fetchone()
        resolution_count = conn.execute(
            "SELECT COUNT(*) FROM review_resolutions"
        ).fetchone()[0]
    assert restored_fact["confirmed_by_user"] == 0
    assert confirmation_action["status"] == "failed"
    assert resolution_count == 0


def test_legacy_wiki_answer_selection_records_exact_resolutions_and_undo(
    tmp_path: Path,
) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()
    left, right = insert_legacy_answer_question(
        paths, question_id="question_legacy_selection"
    )

    with running_ui(paths) as (host, port, token):
        status, body = request_json(
            host,
            port,
            token,
            "POST",
            "/api/wiki/questions/question_legacy_selection/answer",
            {"selected_fact_id": right["id"], "answer": "Keep the current fact."},
        )

    assert status == 200
    assert body["question"]["status"] == "answered"
    assert body["question"]["decided_by"] == "human"
    assert body["question"]["answer"]["decision"] == "manual_selection"
    assert body["undo_handle"]["kind"] == "legacy_question_answer"
    assert body["undo_handle"]["undo_guard"]["fingerprint"]
    with connection(paths.sqlite_path) as conn:
        fact_states = {
            row["id"]: (row["status"], row["confirmed_by_user"])
            for row in conn.execute(
                "SELECT id, status, confirmed_by_user FROM facts WHERE id IN (?, ?)",
                (left["id"], right["id"]),
            )
        }
        resolutions = list(
            conn.execute(
                """
                SELECT disposition, revoked_at
                FROM review_resolutions
                WHERE source_item_kind = 'question' AND source_item_id = ?
                ORDER BY disposition
                """,
                ("question_legacy_selection",),
            )
        )
    assert fact_states == {
        left["id"]: ("retracted", 0),
        right["id"]: ("active", 1),
    }
    assert [(row["disposition"], row["revoked_at"]) for row in resolutions] == [
        ("keep", None),
        ("reject", None),
    ]

    undone = ui_server.ui_queue_undo(paths, {"undo_handle": body["undo_handle"]})
    assert undone["status"] == "undone"
    with connection(paths.sqlite_path) as conn:
        restored = {
            row["id"]: row["status"]
            for row in conn.execute(
                "SELECT id, status FROM facts WHERE id IN (?, ?)",
                (left["id"], right["id"]),
            )
        }
        active_resolution_count = conn.execute(
            """
            SELECT COUNT(*) FROM review_resolutions
            WHERE source_item_id = ? AND revoked_at IS NULL
            """,
            ("question_legacy_selection",),
        ).fetchone()[0]
    assert restored == {left["id"]: "conflicted", right["id"]: "conflicted"}
    assert active_resolution_count == 0


def test_question_actions_undo_restores_exact_answer_page_snapshot(
    tmp_path: Path,
) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()
    left, right = insert_legacy_answer_question(
        paths, question_id="question_actions_page_restore"
    )
    page = paths.wiki / str(right["page_hint"])
    assert not page.exists()

    decision = ui_server.ui_queue_decision(
        paths,
        "question_actions_page_restore",
        {"decision": "select_fact", "selected_fact_id": right["id"]},
    )

    handle = decision["undo_handle"]
    assert handle["kind"] == "question_actions"
    assert handle["page_hints"] == [right["page_hint"]]
    assert handle["page_snapshot_ids"]
    assert page.exists()

    undone = ui_server.ui_queue_undo(paths, {"undo_handle": handle})

    assert undone["status"] == "undone"
    assert "projection_status" not in undone
    assert not page.exists()
    with connection(paths.sqlite_path) as conn:
        restored = {
            row["id"]: row["status"]
            for row in conn.execute(
                "SELECT id, status FROM facts WHERE id IN (?, ?)",
                (left["id"], right["id"]),
            )
        }
    assert restored == {left["id"]: "conflicted", right["id"]: "conflicted"}


@pytest.mark.parametrize(
    "before_markdown",
    [None, "Exact page content from before the review.\nSecond line.\n"],
    ids=["previously_absent", "previously_present"],
)
def test_question_actions_undo_restores_page_written_before_projection_exception(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    before_markdown: str | None,
) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()
    left, right = insert_legacy_answer_question(
        paths, question_id="question_projection_exception_restore"
    )
    page_hint = str(right["page_hint"])
    page = paths.wiki / page_hint
    if before_markdown is not None:
        page.parent.mkdir(parents=True, exist_ok=True)
        page.write_text(before_markdown, encoding="utf-8")
    partial_projection = "Partial managed projection written before failure.\n"

    def write_then_fail(
        actual_paths: BrainPaths,
        *,
        page_hints: list[str],
        overwrite_existing: bool = False,
    ) -> dict[str, object]:
        del overwrite_existing
        target = actual_paths.wiki / page_hints[0]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(partial_projection, encoding="utf-8")
        raise RuntimeError("injected failure after managed page write")

    monkeypatch.setattr(wiki_facts, "curate_managed_pages", write_then_fail)

    decision = ui_server.ui_queue_decision(
        paths,
        "question_projection_exception_restore",
        {"decision": "select_fact", "selected_fact_id": right["id"]},
    )

    result = decision["result"]
    handle = decision["undo_handle"]
    page_result = result["curation"]["pages"][0]
    assert result["status"] == "committed_with_projection_warning"
    assert result["curation"]["projection_status"] == "failed"
    assert "injected failure after managed page write" in result["warnings"][0]
    assert page_result["recovery_status"] == "snapshot_recorded"
    assert handle["page_snapshot_ids"] == [page_result["snapshot_id"]]
    assert page.read_text(encoding="utf-8") == partial_projection

    undone = ui_server.ui_queue_undo(paths, {"undo_handle": handle})

    assert undone["status"] == "undone"
    assert "projection_status" not in undone
    assert page.exists() is (before_markdown is not None)
    if before_markdown is not None:
        assert page.read_text(encoding="utf-8") == before_markdown
    with connection(paths.sqlite_path) as conn:
        restored = {
            row["id"]: row["status"]
            for row in conn.execute(
                "SELECT id, status FROM facts WHERE id IN (?, ?)",
                (left["id"], right["id"]),
            )
        }
    assert restored == {left["id"]: "conflicted", right["id"]: "conflicted"}


@pytest.mark.parametrize(
    "before_markdown",
    [None, "Exact page content before compensated projection.\n"],
    ids=["previously_absent", "previously_present"],
)
def test_managed_curator_compensates_downstream_exception_before_queue_undo(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    before_markdown: str | None,
) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()
    left, right = insert_legacy_answer_question(
        paths, question_id="question_downstream_projection_exception"
    )
    page_hint = str(right["page_hint"])
    page = paths.wiki / page_hint
    if before_markdown is not None:
        page.parent.mkdir(parents=True, exist_ok=True)
        page.write_text(before_markdown, encoding="utf-8")
    with connection(paths.sqlite_path) as conn:
        if before_markdown is not None:
            conn.execute(
                """
                INSERT INTO wiki_pages(
                  id, title, page_type, status, path, source_ids, related, tags,
                  created_at, updated_at, managed, fact_ids
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    wiki_facts.stable_page_id(
                        page_hint, wiki_facts.human_title_for_path(page_hint)
                    ),
                    "Exact pre-projection index title",
                    "reference",
                    "archived",
                    str(page),
                    dumps(["document:pre-projection-index"]),
                    dumps(["concepts/pre-projection-related.md"]),
                    dumps(["pre-projection-index"]),
                    "2025-01-02",
                    "2025-03-04",
                    0,
                    dumps([left["id"]]),
                ),
            )
        index_before = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM wiki_pages WHERE path = ? ORDER BY id", (str(page),)
            )
        ]
    observed_written_page: list[str] = []

    def fail_after_page_write(
        _paths: BrainPaths, pages: list[dict[str, object]]
    ) -> None:
        assert pages[0]["written"] is True
        observed_written_page.append(page.read_text(encoding="utf-8"))
        raise RuntimeError("injected downstream index failure")

    monkeypatch.setattr(wiki_facts, "sync_managed_page_index", fail_after_page_write)

    decision = ui_server.ui_queue_decision(
        paths,
        "question_downstream_projection_exception",
        {
            "decision": "select_fact",
            "selected_fact_id": right["id"],
            "overwrite_existing": True,
        },
    )

    result = decision["result"]
    handle = decision["undo_handle"]
    assert observed_written_page
    assert result["status"] == "committed_with_projection_warning"
    assert result["curation"]["projection_status"] == "failed"
    assert "injected downstream index failure" in result["warnings"][0]
    assert result["curation"]["pages"][0]["recovery_status"] == (
        "unchanged_or_compensated"
    )
    assert handle["page_snapshot_ids"] == []
    assert page.exists() is (before_markdown is not None)
    if before_markdown is not None:
        assert page.read_text(encoding="utf-8") == before_markdown
    with connection(paths.sqlite_path) as conn:
        index_after_failure = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM wiki_pages WHERE path = ? ORDER BY id", (str(page),)
            )
        ]
    assert index_after_failure == index_before

    undone = ui_server.ui_queue_undo(paths, {"undo_handle": handle})

    assert undone["status"] == "undone"
    assert "projection_status" not in undone
    assert page.exists() is (before_markdown is not None)
    if before_markdown is not None:
        assert page.read_text(encoding="utf-8") == before_markdown
    with connection(paths.sqlite_path) as conn:
        restored = {
            row["id"]: row["status"]
            for row in conn.execute(
                "SELECT id, status FROM facts WHERE id IN (?, ?)",
                (left["id"], right["id"]),
            )
        }
        index_after_undo = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM wiki_pages WHERE path = ? ORDER BY id", (str(page),)
            )
        ]
    assert restored == {left["id"]: "conflicted", right["id"]: "conflicted"}
    assert index_after_undo == index_before


@pytest.mark.parametrize("answer_kind", ["manual_selection", "manual_answer"])
def test_legacy_wiki_answer_accepts_incomplete_open_question_only_for_manual_answer(
    tmp_path: Path, answer_kind: str
) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()
    left, right = insert_legacy_answer_question(
        paths, question_id=f"question_incomplete_{answer_kind}"
    )
    with connection(paths.sqlite_path) as conn:
        conn.execute(
            """
            UPDATE facts
            SET source_ids = '[]', source_spans = '[]', evidence_quote = NULL,
                observed_at = NULL, metadata = '{}'
            WHERE id IN (?, ?)
            """,
            (left["id"], right["id"]),
        )

    target = ui_server.find_queue_target(paths, f"question_incomplete_{answer_kind}")
    card = ui_server.queue_card_for_target(paths, target)
    assert card["approvable"] is False
    assert card["blocking_code"] == "missing_evidence"
    assert all(fact["source_date"] is None for fact in card["alternatives"])

    payload = (
        {"selected_fact_id": right["id"]}
        if answer_kind == "manual_selection"
        else {"answer": "The human supplied the missing authoritative context."}
    )
    with running_ui(paths) as (host, port, token):
        blocked_status, _blocked = request_json(
            host,
            port,
            token,
            "POST",
            f"/api/queue/question_incomplete_{answer_kind}/decision",
            {"decision": "both_true"},
        )
        if answer_kind == "manual_selection":
            invalid_status, _invalid = request_json(
                host,
                port,
                token,
                "POST",
                f"/api/wiki/questions/question_incomplete_{answer_kind}/answer",
                {"selected_fact_id": "fact_outside_question"},
            )
            assert invalid_status == 400
        status, body = request_json(
            host,
            port,
            token,
            "POST",
            f"/api/wiki/questions/question_incomplete_{answer_kind}/answer",
            payload,
        )
        closed_status, _closed = request_json(
            host,
            port,
            token,
            "POST",
            f"/api/wiki/questions/question_incomplete_{answer_kind}/answer",
            payload,
        )

    assert blocked_status == 400
    assert status == 200
    assert body["question"]["status"] == "answered"
    assert body["question"]["answer"]["decision"] == answer_kind
    assert closed_status == 400


def test_legacy_wiki_answer_undo_page_drift_leaves_review_state_unchanged(
    tmp_path: Path,
) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()
    left, right = insert_legacy_answer_question(
        paths, question_id="question_legacy_stale_page"
    )
    answer = ui_server.ui_answer_wiki_question(
        paths,
        "question_legacy_stale_page",
        {"selected_fact_id": right["id"]},
    )
    snapshot_ids = answer["undo_handle"]["page_snapshot_ids"]
    assert snapshot_ids
    page = paths.wiki / str(right["page_hint"])
    drifted_markdown = page.read_text(encoding="utf-8") + "\nHuman edit after review.\n"
    page.write_text(drifted_markdown, encoding="utf-8")

    def review_state() -> dict[str, object]:
        with connection(paths.sqlite_path) as conn:
            return {
                "facts": [
                    tuple(row)
                    for row in conn.execute(
                        "SELECT * FROM facts WHERE id IN (?, ?) ORDER BY id",
                        (left["id"], right["id"]),
                    )
                ],
                "question": tuple(
                    conn.execute(
                        "SELECT * FROM open_questions WHERE id = ?",
                        ("question_legacy_stale_page",),
                    ).fetchone()
                ),
                "resolutions": [
                    tuple(row)
                    for row in conn.execute(
                        "SELECT * FROM review_resolutions ORDER BY id"
                    )
                ],
                "actions": [
                    tuple(row)
                    for row in conn.execute("SELECT * FROM cos_actions ORDER BY id")
                ],
            }

    before = review_state()
    with pytest.raises(ui_server.BadRequestError, match="managed page changed"):
        ui_server.ui_queue_undo(paths, {"undo_handle": answer["undo_handle"]})

    assert review_state() == before
    assert page.read_text(encoding="utf-8") == drifted_markdown


def test_legacy_wiki_free_text_answer_rolls_back_if_resolution_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()
    left, right = insert_legacy_answer_question(
        paths, question_id="question_legacy_manual_failure"
    )
    write_resolution = ui_server.record_question_review_resolution

    def fail_after_resolution(*args: object, **kwargs: object) -> None:
        write_resolution(*args, **kwargs)
        raise RuntimeError("injected legacy answer resolution failure")

    monkeypatch.setattr(
        ui_server, "record_question_review_resolution", fail_after_resolution
    )
    with pytest.raises(ui_server.BadRequestError, match="decision was rolled back"):
        ui_server.ui_answer_wiki_question(
            paths,
            "question_legacy_manual_failure",
            {"answer": "The human supplied a corrected replacement fact."},
        )

    with connection(paths.sqlite_path) as conn:
        fact_states = {
            row["id"]: row["status"]
            for row in conn.execute(
                "SELECT id, status FROM facts WHERE id IN (?, ?)",
                (left["id"], right["id"]),
            )
        }
        manual_count = conn.execute(
            """
            SELECT COUNT(*) FROM facts
            WHERE source_ids LIKE '%manual:question:question_legacy_manual_failure%'
            """
        ).fetchone()[0]
        question = conn.execute(
            "SELECT status, decided_by FROM open_questions WHERE id = ?",
            ("question_legacy_manual_failure",),
        ).fetchone()
        active_resolution_count = conn.execute(
            "SELECT COUNT(*) FROM review_resolutions WHERE revoked_at IS NULL"
        ).fetchone()[0]
    page = paths.wiki / "concepts" / "review-workflow.md"
    assert fact_states == {left["id"]: "conflicted", right["id"]: "conflicted"}
    assert manual_count == 0
    assert question["status"] == "open"
    assert question["decided_by"] is None
    assert active_resolution_count == 0
    assert not page.exists()


def test_direct_queue_action_approve_undo_reopens_original_issue(
    tmp_path: Path,
) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()
    action = propose_action(
        paths,
        "edit_contract",
        action_payload={
            "contract": {
                "id": "contract_undo_review",
                "page_hint": "concepts/undo-review.md",
                "canonical_entity": "concept:undo-review",
                "page_scope": "review",
                "retrieval_purpose": "Verify queue undo.",
                "what_belongs_here": "Undo evidence.",
                "what_does_not_belong_here": "Unrelated evidence.",
                "freshness_policy": "manual",
                "related_pages": [],
                "version": 1,
                "status": "active",
            }
        },
        action_features={"reversible": True, "affected_fact_count": 0},
        target_page_paths=["concepts/undo-review.md"],
        proposed_by="test",
        risk_tier="low",
    )

    with running_ui(paths) as (host, port, token):
        decision_status, decision = request_json(
            host,
            port,
            token,
            "POST",
            f"/api/queue/{action['id']}/decision",
            {"decision": "approve"},
        )
        undo_status, _undo = request_json(
            host,
            port,
            token,
            "POST",
            "/api/queue/undo",
            {"undo_handle": decision["undo_handle"]},
        )
        queue_status, queue = request_json(
            host, port, token, "GET", "/api/queue?kind=topology"
        )

    assert decision_status == 200
    assert decision["result"]["action"]["status"] == "applied"
    assert undo_status == 200
    assert queue_status == 200
    assert [item["id"] for item in queue["items"]] == [action["id"]]
    with connection(paths.sqlite_path) as conn:
        current = conn.execute(
            "SELECT status FROM cos_actions WHERE id = ?", (action["id"],)
        ).fetchone()
        resolutions = conn.execute(
            """
            SELECT disposition, revoked_at
            FROM review_resolutions
            WHERE source_item_kind = 'action' AND source_item_id = ?
            """,
            (action["id"],),
        ).fetchall()
    assert current["status"] == "proposed"
    assert [
        (row["disposition"], row["revoked_at"] is not None) for row in resolutions
    ] == [("keep", True)]


@pytest.mark.parametrize(
    ("decision_name", "drift_candidate_sibling"),
    [
        ("approve", False),
        ("reject", False),
        ("approve", True),
        ("reject", True),
    ],
)
def test_direct_queue_decision_undo_restores_candidate_sibling_issue(
    tmp_path: Path, decision_name: str, drift_candidate_sibling: bool
) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()
    candidate_key = "edit_contract:concepts/candidate-undo.md"
    contract = {
        "id": "contract_candidate_reject_undo",
        "page_hint": "concepts/candidate-undo.md",
        "canonical_entity": "concept:candidate-undo",
        "page_scope": "review",
        "retrieval_purpose": "Verify candidate rejection undo.",
        "what_belongs_here": "Candidate review evidence.",
        "what_does_not_belong_here": "Unrelated evidence.",
        "freshness_policy": "manual",
        "related_pages": [],
        "version": 1,
        "status": "active",
    }
    selected = propose_action(
        paths,
        "edit_contract",
        action_payload={"contract": contract},
        action_features={"candidate_key": candidate_key, "reversible": True},
        target_page_paths=["concepts/candidate-undo.md"],
        proposed_by="test",
        risk_tier="low",
    )
    sibling = propose_action(
        paths,
        "edit_contract",
        action_payload={"contract": contract},
        action_features={"reversible": True},
        target_page_paths=["concepts/candidate-undo.md"],
        proposed_by="legacy_test",
        risk_tier="low",
    )
    previous_answer = {"draft": "keep this review note"}
    with connection(paths.sqlite_path) as conn:
        features = json.loads(
            conn.execute(
                "SELECT action_features FROM cos_actions WHERE id = ?",
                (sibling["id"],),
            ).fetchone()[0]
        )
        features["candidate_key"] = candidate_key
        conn.execute(
            "UPDATE cos_actions SET action_features = ?, status = 'needs_human' WHERE id = ?",
            (dumps(features), sibling["id"]),
        )
        conn.execute(
            """
            INSERT INTO open_questions(
              id, kind, fact_ids, question, options, status, answer, context,
              action_id, recommended_action, risk_tier, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "question_candidate_reject_undo_sibling",
                "policy_escalation",
                "[]",
                "Review duplicate candidate.",
                "[]",
                "needs_human",
                dumps(previous_answer),
                "{}",
                sibling["id"],
                "{}",
                "low",
                "2026-07-31T12:00:00+00:00",
            ),
        )

    with running_ui(paths) as (host, port, token):
        decision_status, decision = request_json(
            host,
            port,
            token,
            "POST",
            f"/api/queue/{selected['id']}/decision",
            {"decision": decision_name, "reason": "Not the right candidate."},
        )
        if drift_candidate_sibling:
            with connection(paths.sqlite_path) as conn:
                conn.execute(
                    """
                    UPDATE open_questions
                    SET status = 'answered', answer = '{"decision":"newer_review"}',
                        answered_at = '2026-07-31T12:05:00+00:00',
                        decided_by = 'human'
                    WHERE id = 'question_candidate_reject_undo_sibling'
                    """
                )
        undo_status, undo = request_json(
            host,
            port,
            token,
            "POST",
            "/api/queue/undo",
            {"undo_handle": decision["undo_handle"]},
        )

    assert decision_status == 200
    assert undo_status == (400 if drift_candidate_sibling else 200)
    if drift_candidate_sibling:
        assert "stale" in str(undo.get("error") or "")
    else:
        assert undo["status"] == "undone"
    with connection(paths.sqlite_path) as conn:
        selected_row = conn.execute(
            "SELECT status FROM cos_actions WHERE id = ?", (selected["id"],)
        ).fetchone()
        sibling_row = conn.execute(
            "SELECT status, evidence_json FROM cos_actions WHERE id = ?",
            (sibling["id"],),
        ).fetchone()
        question = conn.execute(
            """
            SELECT status, answer, answered_at, decided_by
            FROM open_questions WHERE id = ?
            """,
            ("question_candidate_reject_undo_sibling",),
        ).fetchone()
    if drift_candidate_sibling:
        assert selected_row["status"] == (
            "applied" if decision_name == "approve" else "rejected"
        )
        assert sibling_row["status"] == "dismissed"
        assert "candidate_superseded" in json.loads(sibling_row["evidence_json"])
        assert question["status"] == "answered"
        assert json.loads(question["answer"]) == {"decision": "newer_review"}
        assert question["decided_by"] == "human"
    else:
        assert selected_row["status"] == "proposed"
        assert sibling_row["status"] == "needs_human"
        assert "candidate_superseded" not in json.loads(sibling_row["evidence_json"])
        assert question["status"] == "needs_human"
        assert json.loads(question["answer"]) == previous_answer
        assert question["answered_at"] is None
        assert question["decided_by"] is None


def test_queue_undo_rejects_tampered_embedded_action_state(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()
    action = propose_action(
        paths,
        "edit_contract",
        action_payload={
            "contract": {
                "id": "contract_tampered_undo",
                "page_hint": "concepts/tampered-undo.md",
                "canonical_entity": "concept:tampered-undo",
                "page_scope": "review",
                "retrieval_purpose": "Reject a forged undo snapshot.",
                "what_belongs_here": "Undo integrity evidence.",
                "what_does_not_belong_here": "Unrelated evidence.",
                "freshness_policy": "manual",
                "related_pages": [],
                "version": 1,
                "status": "active",
            }
        },
        action_features={"reversible": True, "affected_fact_count": 0},
        target_page_paths=["concepts/tampered-undo.md"],
        proposed_by="test",
        risk_tier="low",
    )
    decision = ui_server.ui_queue_decision(
        paths,
        action["id"],
        {"decision": "reject"},
    )
    tampered = json.loads(json.dumps(decision["undo_handle"]))
    tampered["action"]["status"] = "applied"

    with pytest.raises(ui_server.BadRequestError, match="handle is stale"):
        ui_server.ui_queue_undo(paths, {"undo_handle": tampered})

    with connection(paths.sqlite_path) as conn:
        current_action = conn.execute(
            "SELECT status FROM cos_actions WHERE id = ?", (action["id"],)
        ).fetchone()
        contract_count = conn.execute(
            "SELECT COUNT(*) FROM page_contracts WHERE id = 'contract_tampered_undo'"
        ).fetchone()[0]
        active_resolution = conn.execute(
            """
            SELECT disposition FROM review_resolutions
            WHERE revoked_at IS NULL AND source_item_kind = 'action'
              AND source_item_id = ?
            """,
            (action["id"],),
        ).fetchone()
    assert current_action["status"] == "rejected"
    assert contract_count == 0
    assert active_resolution["disposition"] == "reject"


def test_failed_direct_action_undo_leaves_no_revert_residue(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()
    action = propose_action(
        paths,
        "edit_contract",
        action_payload={
            "contract": {
                "id": "contract_direct_undo_drift",
                "page_hint": "concepts/direct-undo-drift.md",
                "canonical_entity": "concept:direct-undo-drift",
                "page_scope": "review",
                "retrieval_purpose": "Exercise guarded direct undo.",
                "what_belongs_here": "Undo evidence.",
                "what_does_not_belong_here": "Unrelated evidence.",
                "freshness_policy": "manual",
                "related_pages": [],
                "version": 1,
                "status": "active",
            }
        },
        action_features={"reversible": True, "affected_fact_count": 0},
        target_page_paths=["concepts/direct-undo-drift.md"],
        proposed_by="test",
        risk_tier="low",
    )
    decision = ui_server.ui_queue_decision(paths, action["id"], {"decision": "approve"})
    with connection(paths.sqlite_path) as conn:
        conn.execute(
            """
            UPDATE page_contracts SET retrieval_purpose = ? WHERE id = ?
            """,
            ("A newer decision changed this contract.", "contract_direct_undo_drift"),
        )

    with pytest.raises(ui_server.BadRequestError, match="handle is stale"):
        ui_server.ui_queue_undo(paths, {"undo_handle": decision["undo_handle"]})

    with connection(paths.sqlite_path) as conn:
        current_action = conn.execute(
            "SELECT status FROM cos_actions WHERE id = ?", (action["id"],)
        ).fetchone()
        contract = conn.execute(
            "SELECT retrieval_purpose FROM page_contracts WHERE id = ?",
            ("contract_direct_undo_drift",),
        ).fetchone()
        residue_count = conn.execute(
            """
            SELECT COUNT(*) FROM open_questions
            WHERE action_id = ? AND kind = 'revert_drift'
            """,
            (action["id"],),
        ).fetchone()[0]
        active_resolution = conn.execute(
            """
            SELECT disposition FROM review_resolutions
            WHERE revoked_at IS NULL AND source_item_kind = 'action'
              AND source_item_id = ?
            """,
            (action["id"],),
        ).fetchone()
    assert current_action["status"] == "applied"
    assert contract["retrieval_purpose"] == "A newer decision changed this contract."
    assert residue_count == 0
    assert active_resolution["disposition"] == "keep"


def test_queue_undo_rolls_back_inverse_when_resolution_revoke_conflicts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()
    action = propose_action(
        paths,
        "edit_contract",
        action_payload={
            "contract": {
                "id": "contract_atomic_undo_ledger",
                "page_hint": "concepts/atomic-undo-ledger.md",
                "canonical_entity": "concept:atomic-undo-ledger",
                "page_scope": "review",
                "retrieval_purpose": "Keep queue undo atomic with its ledger.",
                "what_belongs_here": "Atomic undo evidence.",
                "what_does_not_belong_here": "Unrelated evidence.",
                "freshness_policy": "manual",
                "related_pages": [],
                "version": 1,
                "status": "active",
            }
        },
        action_features={"reversible": True, "affected_fact_count": 0},
        target_page_paths=["concepts/atomic-undo-ledger.md"],
        proposed_by="test",
        risk_tier="low",
    )
    decision = ui_server.ui_queue_decision(paths, action["id"], {"decision": "approve"})

    def fail_revoke(*_args: object, **_kwargs: object) -> None:
        raise ReviewResolutionConflict(
            "review resolution changed; undo was not applied"
        )

    monkeypatch.setattr(
        queue_undo_transaction,
        "revoke_review_resolution",
        fail_revoke,
    )
    with pytest.raises(ui_server.BadRequestError, match="resolution changed"):
        ui_server.ui_queue_undo(paths, {"undo_handle": decision["undo_handle"]})

    resolution_id = decision["undo_handle"]["review_resolution_ids"][0]
    with connection(paths.sqlite_path) as conn:
        current_action = conn.execute(
            "SELECT status FROM cos_actions WHERE id = ?", (action["id"],)
        ).fetchone()
        contract_count = conn.execute(
            "SELECT COUNT(*) FROM page_contracts WHERE id = ?",
            ("contract_atomic_undo_ledger",),
        ).fetchone()[0]
        resolution = conn.execute(
            "SELECT revoked_at FROM review_resolutions WHERE id = ?",
            (resolution_id,),
        ).fetchone()
    assert current_action["status"] == "applied"
    assert contract_count == 1
    assert resolution["revoked_at"] is None


def test_queue_undo_rolls_back_earlier_inverse_when_later_inverse_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()
    actions: list[dict[str, object]] = []
    for suffix in ("first", "second"):
        action = propose_action(
            paths,
            "edit_contract",
            action_payload={
                "contract": {
                    "id": f"contract_atomic_batch_{suffix}",
                    "page_hint": f"concepts/atomic-batch-{suffix}.md",
                    "canonical_entity": f"concept:atomic-batch-{suffix}",
                    "page_scope": "review",
                    "retrieval_purpose": "Exercise atomic batch undo.",
                    "what_belongs_here": "Atomic undo evidence.",
                    "what_does_not_belong_here": "Unrelated evidence.",
                    "freshness_policy": "manual",
                    "related_pages": [],
                    "version": 1,
                    "status": "active",
                }
            },
            action_features={"reversible": True, "affected_fact_count": 0},
            target_page_paths=[f"concepts/atomic-batch-{suffix}.md"],
            proposed_by="test",
            risk_tier="low",
        )
        actions.append(apply_action(paths, action["id"]))

    handle = {
        "kind": "fact_correction",
        "action_ids": [action["id"] for action in actions],
        "page_hints": [],
        "page_snapshot_ids": [],
    }
    seal_undo_handle(paths, handle)
    original_revert = queue_undo_transaction.safely_revert_action_in_connection
    calls = 0

    def fail_second_revert(
        passed_paths: BrainPaths, conn: object, action_id: str
    ) -> dict[str, object]:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise ReviewUndoError("injected later inverse drift")
        return original_revert(passed_paths, conn, action_id)

    monkeypatch.setattr(
        queue_undo_transaction,
        "safely_revert_action_in_connection",
        fail_second_revert,
    )
    with pytest.raises(ui_server.BadRequestError, match="later inverse drift"):
        ui_server.ui_queue_undo(paths, {"undo_handle": handle})

    with connection(paths.sqlite_path) as conn:
        statuses = [
            conn.execute(
                "SELECT status FROM cos_actions WHERE id = ?", (action["id"],)
            ).fetchone()[0]
            for action in actions
        ]
        contract_count = conn.execute(
            """
            SELECT COUNT(*) FROM page_contracts
            WHERE id IN ('contract_atomic_batch_first',
                         'contract_atomic_batch_second')
            """
        ).fetchone()[0]
    assert statuses == ["applied", "applied"]
    assert contract_count == 2


def test_queue_undo_holds_write_lock_between_inverse_and_ledger_revoke(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()
    action = propose_action(
        paths,
        "edit_contract",
        action_payload={
            "contract": {
                "id": "contract_atomic_undo_lock",
                "page_hint": "concepts/atomic-undo-lock.md",
                "canonical_entity": "concept:atomic-undo-lock",
                "page_scope": "review",
                "retrieval_purpose": "Prove queue undo write serialization.",
                "what_belongs_here": "Atomic undo evidence.",
                "what_does_not_belong_here": "Unrelated evidence.",
                "freshness_policy": "manual",
                "related_pages": [],
                "version": 1,
                "status": "active",
            }
        },
        action_features={"reversible": True, "affected_fact_count": 0},
        target_page_paths=["concepts/atomic-undo-lock.md"],
        proposed_by="test",
        risk_tier="low",
    )
    decision = ui_server.ui_queue_decision(paths, action["id"], {"decision": "reject"})
    entered_gap = threading.Event()
    release_gap = threading.Event()
    original_undo = queue_undo_transaction.undo_queue_database_in_connection

    def pause_after_inverse(
        passed_paths: BrainPaths,
        conn: object,
        handle: dict[str, object],
    ) -> None:
        original_undo(passed_paths, conn, handle)
        entered_gap.set()
        if not release_gap.wait(timeout=5):
            raise AssertionError("test did not release the queue undo transaction")

    monkeypatch.setattr(
        queue_undo_transaction,
        "undo_queue_database_in_connection",
        pause_after_inverse,
    )
    results: list[dict[str, object]] = []
    failures: list[BaseException] = []

    def run_undo() -> None:
        try:
            results.append(
                ui_server.ui_queue_undo(paths, {"undo_handle": decision["undo_handle"]})
            )
        except BaseException as exc:  # pragma: no cover - asserted below
            failures.append(exc)

    worker = threading.Thread(target=run_undo)
    worker.start()
    assert entered_gap.wait(timeout=5)

    contender = sqlite3.connect(paths.sqlite_path, timeout=0)
    try:
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            contender.execute("BEGIN IMMEDIATE")
    finally:
        contender.close()
        release_gap.set()
        worker.join(timeout=5)

    assert not worker.is_alive()
    assert failures == []
    assert results[0]["status"] == "undone"
    resolution_id = decision["undo_handle"]["review_resolution_ids"][0]
    with connection(paths.sqlite_path) as conn:
        current_action = conn.execute(
            "SELECT status FROM cos_actions WHERE id = ?", (action["id"],)
        ).fetchone()
        resolution = conn.execute(
            "SELECT revoked_at FROM review_resolutions WHERE id = ?",
            (resolution_id,),
        ).fetchone()
    assert current_action["status"] == "proposed"
    assert resolution["revoked_at"] is not None


def test_stale_undo_replay_cannot_overwrite_a_newer_decision(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()
    action = propose_action(
        paths,
        "edit_contract",
        action_payload={
            "contract": {
                "id": "contract_stale_undo",
                "page_hint": "concepts/stale-undo.md",
                "canonical_entity": "concept:stale-undo",
                "page_scope": "review",
                "retrieval_purpose": "Exercise stale undo replay.",
                "what_belongs_here": "Undo evidence.",
                "what_does_not_belong_here": "Unrelated evidence.",
                "freshness_policy": "manual",
                "related_pages": [],
                "version": 1,
                "status": "active",
            }
        },
        action_features={"reversible": True, "affected_fact_count": 0},
        target_page_paths=["concepts/stale-undo.md"],
        proposed_by="test",
        risk_tier="low",
    )
    approved = ui_server.ui_queue_decision(paths, action["id"], {"decision": "approve"})
    stale_handle = json.loads(json.dumps(approved["undo_handle"]))
    ui_server.ui_queue_undo(paths, {"undo_handle": approved["undo_handle"]})
    rejected = ui_server.ui_queue_decision(paths, action["id"], {"decision": "reject"})

    with pytest.raises(ui_server.BadRequestError, match="handle is stale"):
        ui_server.ui_queue_undo(paths, {"undo_handle": stale_handle})

    with connection(paths.sqlite_path) as conn:
        current_action = conn.execute(
            "SELECT status FROM cos_actions WHERE id = ?", (action["id"],)
        ).fetchone()
        contract_count = conn.execute(
            "SELECT COUNT(*) FROM page_contracts WHERE id = 'contract_stale_undo'"
        ).fetchone()[0]
        active_resolution = conn.execute(
            """
            SELECT disposition FROM review_resolutions
            WHERE revoked_at IS NULL AND source_item_kind = 'action'
              AND source_item_id = ?
            """,
            (action["id"],),
        ).fetchone()
    assert rejected["result"]["action"]["status"] == "rejected"
    assert current_action["status"] == "rejected"
    assert contract_count == 0
    assert active_resolution["disposition"] == "reject"


def test_audit_revert_undo_refuses_to_overwrite_newer_fact_state(
    tmp_path: Path,
) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()
    fact = review_fact_payload("fact_audit_undo_drift")
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
    record_action_audit(
        paths,
        action["id"],
        "sampled_bad",
        metadata={"rationale": "The original state is wrong."},
    )

    with running_ui(paths) as (host, port, token):
        decision_status, decision = request_json(
            host,
            port,
            token,
            "POST",
            f"/api/queue/{action['id']}/decision",
            {"decision": "revert"},
        )
        replacement = dict(fact)
        replacement.update(
            {
                "statement": "A newer source established a materially different fact.",
                "evidence_quote": "A newer source established a materially different fact.",
                "source_ids": ["manual:newer"],
            }
        )
        replacement_action = apply_action(
            paths,
            propose_action(
                paths,
                "fact_upsert",
                action_payload={"fact": replacement},
                action_features={"truth_mutation": False, "reversible": True},
                target_fact_ids=[fact["id"]],
                target_page_paths=[fact["page_hint"]],
                proposed_by="test",
                risk_tier="low",
            )["id"],
        )
        undo_status, _undo = request_json(
            host,
            port,
            token,
            "POST",
            "/api/queue/undo",
            {"undo_handle": decision["undo_handle"]},
        )

    assert decision_status == 200
    assert replacement_action["status"] == "applied"
    assert undo_status == 400
    with connection(paths.sqlite_path) as conn:
        current_fact = conn.execute(
            "SELECT statement FROM facts WHERE id = ?", (fact["id"],)
        ).fetchone()
        current_action = conn.execute(
            "SELECT status FROM cos_actions WHERE id = ?", (action["id"],)
        ).fetchone()
        active_resolution = conn.execute(
            """
            SELECT disposition FROM review_resolutions
            WHERE revoked_at IS NULL AND source_item_kind = 'audit'
              AND source_item_id = ?
            """,
            (action["id"],),
        ).fetchone()
        residue_count = conn.execute(
            """
            SELECT COUNT(*) FROM open_questions
            WHERE action_id = ? AND kind = 'revert_drift'
            """,
            (action["id"],),
        ).fetchone()[0]
    assert current_fact["statement"] == replacement["statement"]
    assert current_action["status"] == "reverted"
    assert active_resolution["disposition"] == "reject"
    assert residue_count == 0


def test_audit_mark_ok_undo_refuses_after_confirmed_fact_drift(
    tmp_path: Path,
) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()
    fact = review_fact_payload("fact_audit_mark_ok_drift")
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
    record_action_audit(
        paths,
        action["id"],
        "sampled_bad",
        metadata={"rationale": "Confirm or reject this state."},
    )

    with running_ui(paths) as (host, port, token):
        decision_status, decision = request_json(
            host,
            port,
            token,
            "POST",
            f"/api/queue/{action['id']}/decision",
            {"decision": "mark_ok"},
        )
        replacement = dict(fact)
        replacement.update(
            {
                "statement": "New evidence changed the confirmed fact.",
                "evidence_quote": "New evidence changed the confirmed fact.",
                "source_ids": ["manual:changed-after-confirmation"],
            }
        )
        apply_action(
            paths,
            propose_action(
                paths,
                "fact_upsert",
                action_payload={"fact": replacement},
                action_features={"truth_mutation": False, "reversible": True},
                target_fact_ids=[fact["id"]],
                target_page_paths=[fact["page_hint"]],
                proposed_by="test",
                risk_tier="low",
            )["id"],
        )
        undo_status, _undo = request_json(
            host,
            port,
            token,
            "POST",
            "/api/queue/undo",
            {"undo_handle": decision["undo_handle"]},
        )

    assert decision_status == 200
    assert undo_status == 400
    confirmation_action_id = decision["undo_handle"]["confirmation_action_id"]
    with connection(paths.sqlite_path) as conn:
        current_fact = conn.execute(
            "SELECT statement, confirmed_by_user FROM facts WHERE id = ?",
            (fact["id"],),
        ).fetchone()
        current_audit = conn.execute(
            "SELECT audit_status FROM cos_actions WHERE id = ?", (action["id"],)
        ).fetchone()
        active_resolution = conn.execute(
            """
            SELECT disposition FROM review_resolutions
            WHERE revoked_at IS NULL AND source_item_kind = 'audit'
              AND source_item_id = ?
            """,
            (action["id"],),
        ).fetchone()
        confirmation_action = conn.execute(
            "SELECT status FROM cos_actions WHERE id = ?", (confirmation_action_id,)
        ).fetchone()
        residue_count = conn.execute(
            """
            SELECT COUNT(*) FROM open_questions
            WHERE action_id = ? AND kind = 'revert_drift'
            """,
            (confirmation_action_id,),
        ).fetchone()[0]
    assert current_fact["statement"] == replacement["statement"]
    assert bool(current_fact["confirmed_by_user"]) is False
    assert current_audit["audit_status"] == "sampled_ok"
    assert active_resolution["disposition"] == "keep"
    assert confirmation_action["status"] == "applied"
    assert residue_count == 0


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
    regenerated = propose_action(
        paths,
        "fact_upsert",
        action_payload={"fact": fact},
        target_fact_ids=[str(fact["id"])],
        target_page_paths=[str(fact["page_hint"])],
        proposed_by="regenerated-test",
        risk_tier="medium",
    )
    assert regenerated["status"] == "rejected"
    assert regenerated["evidence_json"]["semantic_resolution"]["disposition"] == (
        "reject"
    )
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

    undo = ui_server.ui_queue_undo(paths, {"undo_handle": body["undo_handle"]})

    assert undo["status"] == "undone"
    with connection(paths.sqlite_path) as conn:
        restored_existing = conn.execute(
            "SELECT status, source_ids, metadata FROM facts WHERE id = 'fact_existing'"
        ).fetchone()
        restored_action = conn.execute(
            "SELECT status FROM cos_actions WHERE id = ?", (action["id"],)
        ).fetchone()
        restored_question = conn.execute(
            "SELECT status, decided_by FROM open_questions WHERE id = ?",
            ("question_supports_existing",),
        ).fetchone()
        active_resolutions = conn.execute(
            "SELECT COUNT(*) FROM review_resolutions WHERE revoked_at IS NULL"
        ).fetchone()[0]
    assert json.loads(restored_existing["source_ids"]) == ["manual:existing"]
    assert "supporting_candidates" not in json.loads(restored_existing["metadata"])
    assert restored_action["status"] == "proposed"
    assert restored_question["status"] == "needs_human"
    assert restored_question["decided_by"] is None
    assert active_resolutions == 0


def test_v2_route_human_replacement_overrides_prior_semantic_reject(
    tmp_path: Path,
) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()
    fact = review_fact_payload("fact_routed_override")
    original = propose_action(
        paths,
        "fact_upsert",
        action_payload={"fact": fact},
        target_fact_ids=[str(fact["id"])],
        proposed_by="test",
        risk_tier="medium",
    )
    with connection(paths.sqlite_path) as conn:
        record_review_resolution(
            conn,
            original,
            disposition="reject",
            source_item_kind="question",
            source_item_id="prior_route_question",
        )
    insert_unrouted_question(
        paths,
        question_id="question_route_override",
        fact_id=str(fact["id"]),
    )
    with connection(paths.sqlite_path) as conn:
        conn.execute(
            "UPDATE open_questions SET action_id = ? WHERE id = ?",
            (original["id"], "question_route_override"),
        )

    result = ui_server.ui_queue_decision(
        paths,
        "question_route_override",
        {"decision": "new_page", "page_hint": "concepts/routed-override.md"},
    )

    assert result["result"]["action"]["status"] == "applied"
    assert result["result"]["question"]["status"] == "answered"
    with connection(paths.sqlite_path) as conn:
        original_row = conn.execute(
            "SELECT status FROM cos_actions WHERE id = ?", (original["id"],)
        ).fetchone()
    assert original_row["status"] == "rejected"


@pytest.mark.parametrize("decision", ["route", "new_page"])
def test_v2_route_records_keep_blocks_regeneration_and_undo_restores_review(
    tmp_path: Path,
    decision: str,
) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()
    question_id = f"question_{decision}_semantic_keep"
    fact_id = f"fact_{decision}_semantic_keep"
    approved_page = f"concepts/{decision}-semantic-keep.md"
    insert_unrouted_question(paths, question_id=question_id, fact_id=fact_id)

    result = ui_server.ui_queue_decision(
        paths,
        question_id,
        {"decision": decision, "page_hint": approved_page},
    )

    route_action = result["result"]["action"]
    routed_fact = dict(route_action["evidence_json"]["payload"]["fact"])
    assert route_action["status"] == "applied"
    assert routed_fact["page_hint"] == approved_page
    assert len(result["undo_handle"]["review_resolution_ids"]) == 1
    with connection(paths.sqlite_path) as conn:
        resolution = active_resolution_for_action(conn, route_action)
        persisted = conn.execute(
            "SELECT page_hint, status FROM facts WHERE id = ?", (fact_id,)
        ).fetchone()
    assert resolution is not None
    assert resolution["disposition"] == "keep"
    assert persisted["page_hint"] == approved_page
    assert persisted["status"] == "active"

    regenerated = propose_action(
        paths,
        "fact_upsert",
        action_payload={"fact": {**routed_fact, "id": f"{fact_id}_regenerated"}},
        target_fact_ids=[f"{fact_id}_regenerated"],
        target_page_paths=[approved_page],
        proposed_by="regenerated-after-approved-route",
        risk_tier="medium",
    )

    assert regenerated["status"] == "rejected"
    assert regenerated["evidence_json"]["semantic_resolution"]["disposition"] == (
        "keep"
    )
    assert (
        regenerated["evidence_json"]["semantic_resolution"]["outcome"]
        == "exact_semantic_state_already_kept"
    )

    undone = ui_server.ui_queue_undo(paths, {"undo_handle": result["undo_handle"]})

    assert undone["status"] == "undone"
    with connection(paths.sqlite_path) as conn:
        restored_question = conn.execute(
            "SELECT status, action_id, decided_by FROM open_questions WHERE id = ?",
            (question_id,),
        ).fetchone()
        replacement_status = conn.execute(
            "SELECT status FROM cos_actions WHERE id = ?", (route_action["id"],)
        ).fetchone()["status"]
        regenerated_status = conn.execute(
            "SELECT status FROM cos_actions WHERE id = ?", (regenerated["id"],)
        ).fetchone()["status"]
        fact_count = conn.execute(
            "SELECT COUNT(*) FROM facts WHERE id = ?", (fact_id,)
        ).fetchone()[0]
        revoked_at = conn.execute(
            "SELECT revoked_at FROM review_resolutions WHERE id = ?",
            (resolution["id"],),
        ).fetchone()["revoked_at"]
    assert restored_question["status"] == "needs_human"
    assert restored_question["action_id"] is None
    assert restored_question["decided_by"] is None
    assert replacement_status == "reverted"
    assert regenerated_status == "proposed"
    assert fact_count == 0
    assert revoked_at is not None


def test_v2_question_replacement_failure_keeps_original_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()
    apply_existing_review_fact(paths)
    fact = review_fact_payload("fact_support_failure")
    original = propose_action(
        paths,
        "fact_upsert",
        action_payload={"fact": fact},
        target_fact_ids=[str(fact["id"])],
        proposed_by="test",
        risk_tier="medium",
    )
    insert_review_question_for_action(
        paths,
        question_id="question_support_failure",
        action_id=original["id"],
        fact=fact,
    )

    def rejected_replacement(
        paths: BrainPaths, action_id: str, **_kwargs: object
    ) -> dict[str, object]:
        return {
            **ui_server.get_action_for_queue(paths, action_id),
            "status": "rejected",
        }

    monkeypatch.setattr(ui_server, "apply_action", rejected_replacement)
    with pytest.raises(ui_server.BadRequestError, match="replacement did not apply"):
        ui_server.ui_queue_decision(
            paths,
            "question_support_failure",
            {"decision": "merge_evidence"},
        )

    with connection(paths.sqlite_path) as conn:
        original_row = conn.execute(
            "SELECT status FROM cos_actions WHERE id = ?", (original["id"],)
        ).fetchone()
        question = conn.execute(
            "SELECT status, decided_by FROM open_questions WHERE id = ?",
            ("question_support_failure",),
        ).fetchone()
        resolution_count = conn.execute(
            "SELECT COUNT(*) FROM review_resolutions"
        ).fetchone()[0]
    assert original_row["status"] == "proposed"
    assert question["status"] == "needs_human"
    assert question["decided_by"] is None
    assert resolution_count == 0


def test_support_replacement_rolls_back_when_resolution_write_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()
    apply_existing_review_fact(paths)
    fact = review_fact_payload("fact_support_resolution_failure")
    original = propose_action(
        paths,
        "fact_upsert",
        action_payload={"fact": fact},
        target_fact_ids=[str(fact["id"])],
        proposed_by="test",
        risk_tier="medium",
    )
    insert_review_question_for_action(
        paths,
        question_id="question_support_resolution_failure",
        action_id=original["id"],
        fact=fact,
    )
    write_resolution = ui_server.record_question_review_resolution

    def fail_after_resolution(*args: object, **kwargs: object) -> None:
        write_resolution(*args, **kwargs)
        raise RuntimeError("injected resolution persistence failure")

    monkeypatch.setattr(
        ui_server, "record_question_review_resolution", fail_after_resolution
    )
    with pytest.raises(ui_server.BadRequestError, match="decision was rolled back"):
        ui_server.ui_queue_decision(
            paths,
            "question_support_resolution_failure",
            {"decision": "supports"},
        )

    with connection(paths.sqlite_path) as conn:
        existing = conn.execute(
            "SELECT source_ids, metadata FROM facts WHERE id = 'fact_existing'"
        ).fetchone()
        original_row = conn.execute(
            "SELECT status FROM cos_actions WHERE id = ?", (original["id"],)
        ).fetchone()
        replacement = conn.execute(
            """
            SELECT status FROM cos_actions
            WHERE proposed_by = 'ui_queue_supports_existing'
            ORDER BY created_at DESC LIMIT 1
            """
        ).fetchone()
        question = conn.execute(
            "SELECT status, action_id, decided_by FROM open_questions WHERE id = ?",
            ("question_support_resolution_failure",),
        ).fetchone()
        active_resolutions = conn.execute(
            "SELECT COUNT(*) FROM review_resolutions WHERE revoked_at IS NULL"
        ).fetchone()[0]
    assert json.loads(existing["source_ids"]) == ["manual:existing"]
    assert "supporting_candidates" not in json.loads(existing["metadata"])
    assert original_row["status"] == "proposed"
    assert replacement["status"] == "reverted"
    assert question["status"] == "needs_human"
    assert question["action_id"] == original["id"]
    assert question["decided_by"] is None
    assert active_resolutions == 0


def test_reject_candidate_rolls_back_when_resolution_write_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()
    apply_existing_review_fact(paths)
    fact = review_fact_payload("fact_reject_resolution_failure")
    original = propose_action(
        paths,
        "fact_upsert",
        action_payload={"fact": fact},
        target_fact_ids=[str(fact["id"])],
        proposed_by="test",
        risk_tier="medium",
    )
    insert_review_question_for_action(
        paths,
        question_id="question_reject_resolution_failure",
        action_id=original["id"],
        fact=fact,
    )
    write_resolution = ui_server.record_question_review_resolution

    def fail_after_resolution(*args: object, **kwargs: object) -> None:
        write_resolution(*args, **kwargs)
        raise RuntimeError("injected resolution persistence failure")

    monkeypatch.setattr(
        ui_server, "record_question_review_resolution", fail_after_resolution
    )
    with pytest.raises(ui_server.BadRequestError, match="decision was rolled back"):
        ui_server.ui_queue_decision(
            paths,
            "question_reject_resolution_failure",
            {"decision": "reject_candidate"},
        )

    with connection(paths.sqlite_path) as conn:
        original_row = conn.execute(
            "SELECT status FROM cos_actions WHERE id = ?", (original["id"],)
        ).fetchone()
        question = conn.execute(
            "SELECT status, action_id, decided_by FROM open_questions WHERE id = ?",
            ("question_reject_resolution_failure",),
        ).fetchone()
        active_resolutions = conn.execute(
            "SELECT COUNT(*) FROM review_resolutions WHERE revoked_at IS NULL"
        ).fetchone()[0]
    assert original_row["status"] == "proposed"
    assert question["status"] == "needs_human"
    assert question["action_id"] == original["id"]
    assert question["decided_by"] is None
    assert active_resolutions == 0


def test_route_replacement_rolls_back_when_resolution_write_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()
    fact_id = "fact_route_resolution_failure"
    insert_unrouted_question(
        paths,
        question_id="question_route_resolution_failure",
        fact_id=fact_id,
    )
    write_resolution = ui_server.record_question_review_resolution

    def fail_after_resolution(*args: object, **kwargs: object) -> None:
        write_resolution(*args, **kwargs)
        raise RuntimeError("injected resolution persistence failure")

    monkeypatch.setattr(
        ui_server, "record_question_review_resolution", fail_after_resolution
    )
    with pytest.raises(ui_server.BadRequestError, match="decision was rolled back"):
        ui_server.ui_queue_decision(
            paths,
            "question_route_resolution_failure",
            {"decision": "route", "page_hint": "concepts/resolution-failure.md"},
        )

    with connection(paths.sqlite_path) as conn:
        fact_count = conn.execute(
            "SELECT COUNT(*) FROM facts WHERE id = ?", (fact_id,)
        ).fetchone()[0]
        replacement = conn.execute(
            """
            SELECT status FROM cos_actions
            WHERE proposed_by = 'ui_queue_route'
            ORDER BY created_at DESC LIMIT 1
            """
        ).fetchone()
        question = conn.execute(
            "SELECT status, action_id, decided_by FROM open_questions WHERE id = ?",
            ("question_route_resolution_failure",),
        ).fetchone()
        active_resolutions = conn.execute(
            "SELECT COUNT(*) FROM review_resolutions WHERE revoked_at IS NULL"
        ).fetchone()[0]
    assert fact_count == 0
    assert replacement["status"] == "reverted"
    assert question["status"] == "needs_human"
    assert question["action_id"] is None
    assert question["decided_by"] is None
    assert active_resolutions == 0


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

    regenerated = propose_action(
        paths,
        "fact_upsert",
        action_payload={"fact": fact},
        target_fact_ids=[str(fact["id"])],
        target_page_paths=[str(fact["page_hint"])],
        proposed_by="regenerated-test",
        risk_tier="medium",
    )
    assert regenerated["status"] == "rejected"
    undo = ui_server.ui_queue_undo(paths, {"undo_handle": body["undo_handle"]})
    assert undo["status"] == "undone"
    with connection(paths.sqlite_path) as conn:
        current_count = conn.execute(
            "SELECT COUNT(*) FROM facts WHERE id = 'fact_temporal_current'"
        ).fetchone()[0]
        restored_existing = conn.execute(
            "SELECT status FROM facts WHERE id = 'fact_existing'"
        ).fetchone()
        restored_action = conn.execute(
            "SELECT status FROM cos_actions WHERE id = ?", (action["id"],)
        ).fetchone()
        restored_question = conn.execute(
            "SELECT status, decided_by FROM open_questions WHERE id = ?",
            ("question_temporal_update",),
        ).fetchone()
        active_resolutions = conn.execute(
            "SELECT COUNT(*) FROM review_resolutions WHERE revoked_at IS NULL"
        ).fetchone()[0]
    assert current_count == 0
    assert restored_existing["status"] == "active"
    assert restored_action["status"] == "proposed"
    assert restored_question["status"] == "needs_human"
    assert restored_question["decided_by"] is None
    assert active_resolutions == 0


def test_v2_temporal_replacement_failure_rolls_back_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()
    apply_existing_review_fact(paths)
    fact = review_fact_payload("fact_temporal_failure")
    original = propose_action(
        paths,
        "fact_upsert",
        action_payload={"fact": fact},
        target_fact_ids=[str(fact["id"])],
        proposed_by="test",
        risk_tier="medium",
    )
    insert_review_question_for_action(
        paths,
        question_id="question_temporal_failure",
        action_id=original["id"],
        fact=fact,
    )
    monkeypatch.setattr(
        ui_server,
        "apply_fact_status_action",
        lambda *_args, **_kwargs: {"id": None, "status": "skipped"},
    )

    with pytest.raises(ui_server.BadRequestError, match="replacement did not apply"):
        ui_server.ui_queue_decision(
            paths,
            "question_temporal_failure",
            {"decision": "current_state"},
        )

    with connection(paths.sqlite_path) as conn:
        candidate_count = conn.execute(
            "SELECT COUNT(*) FROM facts WHERE id = ?", (fact["id"],)
        ).fetchone()[0]
        original_row = conn.execute(
            "SELECT status FROM cos_actions WHERE id = ?", (original["id"],)
        ).fetchone()
        question = conn.execute(
            "SELECT status, decided_by FROM open_questions WHERE id = ?",
            ("question_temporal_failure",),
        ).fetchone()
    assert candidate_count == 0
    assert original_row["status"] == "proposed"
    assert question["status"] == "needs_human"
    assert question["decided_by"] is None


def test_temporal_replacement_rolls_back_both_actions_after_apply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()
    apply_existing_review_fact(paths)
    fact = review_fact_payload("fact_temporal_post_apply_failure")
    original = propose_action(
        paths,
        "fact_upsert",
        action_payload={"fact": fact},
        target_fact_ids=[str(fact["id"])],
        proposed_by="test",
        risk_tier="medium",
    )
    insert_review_question_for_action(
        paths,
        question_id="question_temporal_post_apply_failure",
        action_id=original["id"],
        fact=fact,
    )

    def fail_after_replacements(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("injected post-apply failure")

    monkeypatch.setattr(
        ui_server, "reject_linked_review_action", fail_after_replacements
    )
    with pytest.raises(RuntimeError, match="post-apply failure"):
        ui_server.ui_queue_decision(
            paths,
            "question_temporal_post_apply_failure",
            {"decision": "temporal_update"},
        )

    with connection(paths.sqlite_path) as conn:
        candidate_count = conn.execute(
            "SELECT COUNT(*) FROM facts WHERE id = ?", (fact["id"],)
        ).fetchone()[0]
        existing = conn.execute(
            "SELECT status FROM facts WHERE id = 'fact_existing'"
        ).fetchone()
        original_row = conn.execute(
            "SELECT status FROM cos_actions WHERE id = ?", (original["id"],)
        ).fetchone()
        replacement_rows = conn.execute(
            """
            SELECT action_type, status FROM cos_actions
            WHERE proposed_by = 'ui_queue_temporal_update'
            ORDER BY created_at, id
            """
        ).fetchall()
        question = conn.execute(
            "SELECT status, action_id, decided_by FROM open_questions WHERE id = ?",
            ("question_temporal_post_apply_failure",),
        ).fetchone()
        resolution_count = conn.execute(
            "SELECT COUNT(*) FROM review_resolutions"
        ).fetchone()[0]
    assert candidate_count == 0
    assert existing["status"] == "active"
    assert original_row["status"] == "proposed"
    assert {(row["action_type"], row["status"]) for row in replacement_rows} == {
        ("fact_upsert", "reverted"),
        ("fact_supersede", "reverted"),
    }
    assert question["status"] == "needs_human"
    assert question["action_id"] == original["id"]
    assert question["decided_by"] is None
    assert resolution_count == 0


def test_multi_action_undo_rollback_retains_fresh_inverse_and_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()
    facts = [
        review_fact_payload("fact_multi_undo_first"),
        review_fact_payload("fact_multi_undo_second"),
    ]
    actions = [
        apply_action(
            paths,
            propose_action(
                paths,
                "fact_upsert",
                action_payload={"fact": fact},
                action_features={"truth_mutation": False, "reversible": True},
                target_fact_ids=[str(fact["id"])],
                target_page_paths=[str(fact["page_hint"])],
                proposed_by="test",
                risk_tier="low",
            )["id"],
        )
        for fact in facts
    ]
    revert_question_action = question_resolution.safely_revert_question_action

    def fail_second_revert(paths_arg: BrainPaths, action_id: str) -> None:
        if action_id == actions[1]["id"]:
            raise question_resolution.QuestionResolutionError(
                "injected second revert failure"
            )
        revert_question_action(paths_arg, action_id)

    monkeypatch.setattr(
        question_resolution,
        "safely_revert_question_action",
        fail_second_revert,
    )
    with pytest.raises(
        question_resolution.QuestionResolutionError, match="second revert failure"
    ):
        question_resolution.undo_question_actions(
            paths, [str(action["id"]) for action in actions]
        )

    with connection(paths.sqlite_path) as conn:
        first = load_action(conn, str(actions[0]["id"]))
        second = load_action(conn, str(actions[1]["id"]))
        current_hash = target_state_hash(
            conn,
            target_fact_ids=first.get("target_fact_ids") or [],
            target_contract_ids=first.get("target_contract_ids") or [],
            target_page_paths=first.get("target_page_paths") or [],
        )
        fact_count = conn.execute(
            """
            SELECT COUNT(*) FROM facts
            WHERE id IN ('fact_multi_undo_first', 'fact_multi_undo_second')
            """
        ).fetchone()[0]
    assert first["status"] == "applied"
    assert first["inverse_action_json"]["delete_fact_ids"] == ["fact_multi_undo_first"]
    assert first["applied_state_hash"] == current_hash
    assert first["reverted_at"] is None
    assert second["status"] == "applied"
    assert fact_count == 2


def test_v2_question_undo_drift_preserves_resolution_and_closed_state(
    tmp_path: Path,
) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()
    apply_existing_review_fact(paths)
    fact = review_fact_payload("fact_support_drift")
    original = propose_action(
        paths,
        "fact_upsert",
        action_payload={"fact": fact},
        target_fact_ids=[str(fact["id"])],
        proposed_by="test",
        risk_tier="medium",
    )
    insert_review_question_for_action(
        paths,
        question_id="question_support_drift",
        action_id=original["id"],
        fact=fact,
    )
    result = ui_server.ui_queue_decision(
        paths,
        "question_support_drift",
        {"decision": "supports"},
    )
    with connection(paths.sqlite_path) as conn:
        conn.execute(
            "UPDATE facts SET statement = ? WHERE id = 'fact_existing'",
            ("The fact changed after the replacement was applied.",),
        )

    with pytest.raises(ui_server.BadRequestError, match="safely reverted"):
        ui_server.ui_queue_undo(paths, {"undo_handle": result["undo_handle"]})

    with connection(paths.sqlite_path) as conn:
        original_row = conn.execute(
            "SELECT status FROM cos_actions WHERE id = ?", (original["id"],)
        ).fetchone()
        question = conn.execute(
            "SELECT status, decided_by FROM open_questions WHERE id = ?",
            ("question_support_drift",),
        ).fetchone()
        active_resolutions = conn.execute(
            "SELECT COUNT(*) FROM review_resolutions WHERE revoked_at IS NULL"
        ).fetchone()[0]
    assert original_row["status"] == "rejected"
    assert question["status"] == "answered"
    assert question["decided_by"] == "human"
    assert active_resolutions == 1


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
    assert body["undo_handle"]["review_resolution_ids"]
    assert body["undo_handle"]["undo_guard"]["fingerprint"]
    with connection(paths.sqlite_path) as conn:
        fact_row = conn.execute(
            "SELECT statement, status FROM facts WHERE id = ?", (fact["id"],)
        ).fetchone()
        question_row = conn.execute(
            "SELECT status, decided_by FROM open_questions WHERE id = ?",
            ("question_review_apply",),
        ).fetchone()
        resolution_row = conn.execute(
            """
            SELECT id, disposition, revoked_at
            FROM review_resolutions
            WHERE source_item_kind = 'question' AND source_item_id = ?
            """,
            ("question_review_apply",),
        ).fetchone()
    assert fact_row["statement"] == fact["statement"]
    assert fact_row["status"] == "active"
    assert question_row["status"] == "answered"
    assert question_row["decided_by"] == "human"
    assert resolution_row["id"] in body["undo_handle"]["review_resolution_ids"]
    assert resolution_row["disposition"] == "keep"
    assert resolution_row["revoked_at"] is None

    undone = ui_server.ui_queue_undo(paths, {"undo_handle": body["undo_handle"]})
    assert undone["status"] == "undone"
    with connection(paths.sqlite_path) as conn:
        restored_action = conn.execute(
            "SELECT status FROM cos_actions WHERE id = ?", (action["id"],)
        ).fetchone()
        restored_question = conn.execute(
            "SELECT status, decided_by FROM open_questions WHERE id = ?",
            ("question_review_apply",),
        ).fetchone()
        fact_count = conn.execute(
            "SELECT COUNT(*) FROM facts WHERE id = ?", (fact["id"],)
        ).fetchone()[0]
        revoked_at = conn.execute(
            "SELECT revoked_at FROM review_resolutions WHERE id = ?",
            (resolution_row["id"],),
        ).fetchone()[0]
    assert restored_action["status"] == "needs_human"
    assert restored_question["status"] == "needs_human"
    assert restored_question["decided_by"] is None
    assert fact_count == 0
    assert revoked_at is not None


def test_cos_review_dismiss_endpoint_rejects_linked_action(
    tmp_path: Path,
) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()
    fact = review_fact_payload("fact_rejected")
    candidate_key = "fact_upsert:fact_rejected"
    action = propose_action(
        paths,
        "fact_upsert",
        action_payload={"fact": fact},
        action_features={
            "candidate_key": candidate_key,
            "truth_mutation": True,
            "reversible": True,
        },
        target_fact_ids=[str(fact["id"])],
        target_page_paths=[str(fact["page_hint"])],
        proposed_by="test",
        risk_tier="medium",
    )
    sibling = propose_action(
        paths,
        "fact_upsert",
        action_payload={"fact": fact},
        action_features={"truth_mutation": True, "reversible": True},
        target_fact_ids=[str(fact["id"])],
        target_page_paths=[str(fact["page_hint"])],
        proposed_by="legacy_test",
        risk_tier="medium",
    )
    with connection(paths.sqlite_path) as conn:
        features = json.loads(
            conn.execute(
                "SELECT action_features FROM cos_actions WHERE id = ?",
                (sibling["id"],),
            ).fetchone()[0]
        )
        features["candidate_key"] = candidate_key
        conn.execute(
            "UPDATE cos_actions SET action_features = ?, status = 'needs_human' WHERE id = ?",
            (dumps(features), sibling["id"]),
        )
    insert_review_question_for_action(
        paths,
        question_id="question_review_reject",
        action_id=action["id"],
        fact=fact,
    )
    insert_review_question_for_action(
        paths,
        question_id="question_review_reject_sibling",
        action_id=sibling["id"],
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
    assert body["undo_handle"]["review_resolution_ids"]
    assert body["undo_handle"]["undo_guard"]["fingerprint"]
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
        retired_sibling = conn.execute(
            "SELECT status FROM cos_actions WHERE id = ?", (sibling["id"],)
        ).fetchone()
        retired_sibling_question = conn.execute(
            "SELECT status, decided_by FROM open_questions WHERE id = ?",
            ("question_review_reject_sibling",),
        ).fetchone()
        resolution_row = conn.execute(
            """
            SELECT id, disposition, revoked_at
            FROM review_resolutions
            WHERE source_item_kind = 'question' AND source_item_id = ?
            """,
            ("question_review_reject",),
        ).fetchone()
    assert fact_count == 0
    assert question_row["status"] == "dismissed"
    assert question_row["decided_by"] == "human"
    assert retired_sibling["status"] == "dismissed"
    assert retired_sibling_question["status"] == "dismissed"
    assert retired_sibling_question["decided_by"] == "candidate_deduplication"
    assert resolution_row["id"] in body["undo_handle"]["review_resolution_ids"]
    assert resolution_row["disposition"] == "reject"
    assert resolution_row["revoked_at"] is None

    undone = ui_server.ui_queue_undo(paths, {"undo_handle": body["undo_handle"]})
    assert undone["status"] == "undone"
    with connection(paths.sqlite_path) as conn:
        restored_action = conn.execute(
            "SELECT status FROM cos_actions WHERE id = ?", (action["id"],)
        ).fetchone()
        restored_question = conn.execute(
            "SELECT status, decided_by FROM open_questions WHERE id = ?",
            ("question_review_reject",),
        ).fetchone()
        restored_sibling = conn.execute(
            "SELECT status FROM cos_actions WHERE id = ?", (sibling["id"],)
        ).fetchone()
        restored_sibling_question = conn.execute(
            "SELECT status, decided_by FROM open_questions WHERE id = ?",
            ("question_review_reject_sibling",),
        ).fetchone()
        revoked_at = conn.execute(
            "SELECT revoked_at FROM review_resolutions WHERE id = ?",
            (resolution_row["id"],),
        ).fetchone()[0]
    assert restored_action["status"] == "proposed"
    assert restored_question["status"] == "needs_human"
    assert restored_question["decided_by"] is None
    assert restored_sibling["status"] == "needs_human"
    assert restored_sibling_question["status"] == "needs_human"
    assert restored_sibling_question["decided_by"] is None
    assert revoked_at is not None


def test_cos_review_apply_rolls_back_if_semantic_resolution_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()
    fact = review_fact_payload("fact_legacy_apply_resolution_failure")
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
        question_id="question_legacy_apply_resolution_failure",
        action_id=action["id"],
        fact=fact,
    )
    write_resolution = ui_server.record_question_review_resolution

    def fail_after_resolution(*args: object, **kwargs: object) -> None:
        write_resolution(*args, **kwargs)
        raise RuntimeError("injected semantic resolution failure")

    monkeypatch.setattr(
        ui_server, "record_question_review_resolution", fail_after_resolution
    )
    with pytest.raises(ui_server.BadRequestError, match="decision was rolled back"):
        ui_server.ui_apply_cos_question_action(
            paths,
            "question_legacy_apply_resolution_failure",
            {"note": "looks supported"},
        )

    with connection(paths.sqlite_path) as conn:
        action_row = conn.execute(
            "SELECT status FROM cos_actions WHERE id = ?", (action["id"],)
        ).fetchone()
        question_row = conn.execute(
            "SELECT status, decided_by FROM open_questions WHERE id = ?",
            ("question_legacy_apply_resolution_failure",),
        ).fetchone()
        fact_count = conn.execute(
            "SELECT COUNT(*) FROM facts WHERE id = ?", (fact["id"],)
        ).fetchone()[0]
        resolution_rows = conn.execute(
            "SELECT disposition, revoked_at FROM review_resolutions"
        ).fetchall()
    assert action_row["status"] == "needs_human"
    assert question_row["status"] == "needs_human"
    assert question_row["decided_by"] is None
    assert fact_count == 0
    assert [
        (row["disposition"], row["revoked_at"] is not None) for row in resolution_rows
    ] == [("keep", True)]


def test_cos_review_dismiss_rolls_back_if_semantic_resolution_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()
    fact = review_fact_payload("fact_legacy_dismiss_resolution_failure")
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
        question_id="question_legacy_dismiss_resolution_failure",
        action_id=action["id"],
        fact=fact,
    )
    write_resolution = ui_server.record_question_review_resolution

    def fail_after_resolution(*args: object, **kwargs: object) -> None:
        write_resolution(*args, **kwargs)
        raise RuntimeError("injected semantic resolution failure")

    monkeypatch.setattr(
        ui_server, "record_question_review_resolution", fail_after_resolution
    )
    with pytest.raises(ui_server.BadRequestError, match="decision was rolled back"):
        ui_server.ui_dismiss_cos_question(
            paths,
            "question_legacy_dismiss_resolution_failure",
            {"reason": "not supported by quote"},
        )

    with connection(paths.sqlite_path) as conn:
        action_row = conn.execute(
            "SELECT status, evidence_json FROM cos_actions WHERE id = ?",
            (action["id"],),
        ).fetchone()
        question_row = conn.execute(
            "SELECT status, decided_by FROM open_questions WHERE id = ?",
            ("question_legacy_dismiss_resolution_failure",),
        ).fetchone()
        resolution_rows = conn.execute(
            "SELECT disposition, revoked_at FROM review_resolutions"
        ).fetchall()
    assert action_row["status"] == "proposed"
    assert "human_review" not in json.loads(action_row["evidence_json"])
    assert question_row["status"] == "needs_human"
    assert question_row["decided_by"] is None
    assert [
        (row["disposition"], row["revoked_at"] is not None) for row in resolution_rows
    ] == [("reject", True)]


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


def test_wiki_fact_correction_records_exact_resolutions_and_guarded_undo(
    tmp_path: Path,
) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()
    original = existing_review_fact_payload("fact_correction_original")
    apply_action(
        paths,
        propose_action(
            paths,
            "fact_upsert",
            action_payload={"fact": original},
            target_fact_ids=[str(original["id"])],
            target_page_paths=[str(original["page_hint"])],
            proposed_by="test_correction",
            risk_tier="low",
        )["id"],
    )
    wiki_facts.curate_managed_pages(paths, page_hints=[str(original["page_hint"])])

    correction = ui_server.ui_create_wiki_fact_correction(
        paths,
        {
            "page_hint": original["page_hint"],
            "statement": "The corrected workflow now presents complete evidence.",
            "supersede_fact_ids": [original["id"]],
        },
    )

    assert correction["undo_handle"]["kind"] == "fact_correction"
    assert len(correction["undo_handle"]["action_ids"]) == 2
    assert len(correction["undo_handle"]["review_resolution_ids"]) == 2
    assert correction["undo_handle"]["undo_guard"]["fingerprint"]
    with connection(paths.sqlite_path) as conn:
        original_status = conn.execute(
            "SELECT status FROM facts WHERE id = ?", (original["id"],)
        ).fetchone()[0]
        rows = list(
            conn.execute(
                """
                SELECT disposition, decision_payload, revoked_at
                FROM review_resolutions
                WHERE source_item_kind = 'wiki_correction'
                ORDER BY disposition
                """
            )
        )
    assert original_status == "retracted"
    assert [(row["disposition"], row["revoked_at"]) for row in rows] == [
        ("keep", None),
        ("reject", None),
    ]
    reject_payload = json.loads(
        next(row["decision_payload"] for row in rows if row["disposition"] == "reject")
    )
    assert reject_payload["corrected_away_fact_id"] == original["id"]

    undone = ui_server.ui_queue_undo(paths, {"undo_handle": correction["undo_handle"]})

    assert undone["status"] == "undone"
    with connection(paths.sqlite_path) as conn:
        restored_original = conn.execute(
            "SELECT status FROM facts WHERE id = ?", (original["id"],)
        ).fetchone()[0]
        replacement_count = conn.execute(
            "SELECT COUNT(*) FROM facts WHERE id = ?",
            (correction["fact"]["id"],),
        ).fetchone()[0]
        action_statuses = [
            row["status"]
            for row in conn.execute(
                """
                SELECT status FROM cos_actions
                WHERE id IN (?, ?)
                ORDER BY action_type
                """,
                correction["undo_handle"]["action_ids"],
            )
        ]
        active_resolution_count = conn.execute(
            """
            SELECT COUNT(*) FROM review_resolutions
            WHERE source_item_kind = 'wiki_correction' AND revoked_at IS NULL
            """
        ).fetchone()[0]
    markdown = (paths.wiki / str(original["page_hint"])).read_text(encoding="utf-8")
    assert restored_original == "active"
    assert replacement_count == 0
    assert action_statuses == ["reverted", "reverted"]
    assert active_resolution_count == 0
    assert str(original["statement"]) in markdown
    assert "corrected workflow" not in markdown


def test_wiki_fact_correction_undo_page_drift_is_non_mutating(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()
    original = existing_review_fact_payload("fact_correction_page_drift")
    apply_action(
        paths,
        propose_action(
            paths,
            "fact_upsert",
            action_payload={"fact": original},
            target_fact_ids=[str(original["id"])],
            target_page_paths=[str(original["page_hint"])],
            proposed_by="test_correction_page_drift",
            risk_tier="low",
        )["id"],
    )
    correction = ui_server.ui_create_wiki_fact_correction(
        paths,
        {
            "page_hint": original["page_hint"],
            "statement": "The corrected workflow is now complete.",
            "supersede_fact_ids": [original["id"]],
        },
    )
    assert correction["undo_handle"]["page_snapshot_ids"]
    page = paths.wiki / str(original["page_hint"])
    drifted_markdown = page.read_text(encoding="utf-8") + "\nHuman page edit.\n"
    page.write_text(drifted_markdown, encoding="utf-8")

    def correction_state() -> dict[str, object]:
        with connection(paths.sqlite_path) as conn:
            return {
                "facts": [
                    tuple(row)
                    for row in conn.execute("SELECT * FROM facts ORDER BY id")
                ],
                "actions": [
                    tuple(row)
                    for row in conn.execute("SELECT * FROM cos_actions ORDER BY id")
                ],
                "resolutions": [
                    tuple(row)
                    for row in conn.execute(
                        "SELECT * FROM review_resolutions ORDER BY id"
                    )
                ],
                "snapshots": [
                    tuple(row)
                    for row in conn.execute(
                        "SELECT * FROM wiki_page_snapshots ORDER BY id"
                    )
                ],
            }

    before = correction_state()
    with pytest.raises(ui_server.BadRequestError, match="managed page changed"):
        ui_server.ui_queue_undo(paths, {"undo_handle": correction["undo_handle"]})

    assert correction_state() == before
    assert page.read_text(encoding="utf-8") == drifted_markdown


def test_wiki_fact_correction_undo_uses_canonical_career_page_snapshot(
    tmp_path: Path,
) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()
    original = existing_review_fact_payload("fact_correction_career")
    original.update(
        {
            "entity_key": "career:acme:summary",
            "page_hint": "career/acme.md",
            "statement": "The Acme opportunity uses the original process.",
        }
    )
    apply_action(
        paths,
        propose_action(
            paths,
            "fact_upsert",
            action_payload={"fact": original},
            target_fact_ids=[str(original["id"])],
            target_page_paths=[str(original["page_hint"])],
            proposed_by="test_correction_canonical_career",
            risk_tier="low",
        )["id"],
    )
    wiki_facts.curate_managed_pages(paths, page_hints=["career/acme.md"])

    correction = ui_server.ui_create_wiki_fact_correction(
        paths,
        {
            "page_hint": "career/opportunities/acme-senior-pm.md",
            "statement": "The Acme opportunity now uses the corrected process.",
            "supersede_fact_ids": [original["id"]],
        },
    )

    snapshot_ids = [
        page["snapshot_id"]
        for page in correction["curation"]["pages"]
        if page.get("snapshot_id")
    ]
    assert correction["fact"]["page_hint"] == "career/acme.md"
    assert correction["undo_handle"]["page_hints"] == ["career/acme.md"]
    assert correction["undo_handle"]["page_snapshot_ids"] == snapshot_ids

    ui_server.ui_queue_undo(paths, {"undo_handle": correction["undo_handle"]})

    canonical_page = paths.wiki / "career" / "acme.md"
    assert str(original["statement"]) in canonical_page.read_text(encoding="utf-8")
    assert "corrected process" not in canonical_page.read_text(encoding="utf-8")
    assert not (paths.wiki / "career" / "opportunities" / "acme-senior-pm.md").exists()


def test_wiki_fact_correction_compensates_if_resolution_write_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()
    original = existing_review_fact_payload("fact_correction_failure_original")
    apply_action(
        paths,
        propose_action(
            paths,
            "fact_upsert",
            action_payload={"fact": original},
            target_fact_ids=[str(original["id"])],
            target_page_paths=[str(original["page_hint"])],
            proposed_by="test_correction_failure",
            risk_tier="low",
        )["id"],
    )
    write_resolution = wiki_facts.record_review_resolution

    def fail_after_resolution(*args: object, **kwargs: object) -> tuple[object, bool]:
        write_resolution(*args, **kwargs)
        raise RuntimeError("injected correction resolution failure")

    monkeypatch.setattr(wiki_facts, "record_review_resolution", fail_after_resolution)
    with pytest.raises(RuntimeError, match="correction resolution failure"):
        ui_server.ui_create_wiki_fact_correction(
            paths,
            {
                "page_hint": original["page_hint"],
                "statement": "A correction whose ledger write must fail.",
                "supersede_fact_ids": [original["id"]],
            },
        )

    with connection(paths.sqlite_path) as conn:
        original_status = conn.execute(
            "SELECT status FROM facts WHERE id = ?", (original["id"],)
        ).fetchone()[0]
        replacement_count = conn.execute(
            """
            SELECT COUNT(*) FROM facts
            WHERE source_ids LIKE '%manual:chief-of-staff:%'
            """
        ).fetchone()[0]
        correction_action_statuses = [
            row["status"]
            for row in conn.execute(
                """
                SELECT status FROM cos_actions
                WHERE proposed_by = 'chief_of_staff_correction'
                ORDER BY created_at, id
                """
            )
        ]
        active_resolution_count = conn.execute(
            "SELECT COUNT(*) FROM review_resolutions WHERE revoked_at IS NULL"
        ).fetchone()[0]
    assert original_status == "active"
    assert replacement_count == 0
    assert sorted(correction_action_statuses) == ["failed", "reverted"]
    assert active_resolution_count == 0


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
