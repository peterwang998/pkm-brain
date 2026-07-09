from __future__ import annotations

import http.client
import json
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from pkm_brain import ui_server
from pkm_brain.cos_actions import decide_action, propose_action, record_action_audit
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


def insert_review_question_for_action(
    paths: BrainPaths, *, question_id: str, action_id: str, fact: dict[str, object]
) -> None:
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
                            "fact_id": "fact_existing",
                            "statement": "The old review workflow did not show enough evidence.",
                            "evidence_quote": "The old review workflow did not show enough evidence.",
                            "source_ids": ["manual:existing"],
                        },
                    ]
                ),
                "needs_human",
                dumps({"action_id": action_id, "counterpart_fact_ids": ["fact_existing"]}),
                action_id,
                dumps({"action_type": "fact_upsert", "payload": {"fact": fact}}),
                "medium",
                "2026-07-06T10:01:00+00:00",
            ),
        )


def insert_unrouted_question(paths: BrainPaths, *, question_id: str, fact_id: str) -> None:
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


def test_v2_static_shell_serves_without_auth(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")

    with running_ui(paths) as (host, port, _token):
        root_status, root_body, root_headers = request_raw(host, port, "GET", "/")
        js_status, js_body, js_headers = request_raw(host, port, "GET", "/ui/app.js")

    assert root_status == 200
    assert "<script type=\"module\" src=\"/ui/app.js\"></script>" in root_body
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
    assert queue_status == 200
    conflict = next(item for item in queue["items"] if item["id"] == "question_v2_queue")
    assert conflict["group"] == "conflicts"
    assert conflict["candidate"]["statement"] == fact["statement"]
    assert conflict["counterparts"][0]["statement"] == "The old review workflow did not show enough evidence."
    memory = next(item for item in queue["items"] if item["group"] == "memories")
    assert memory["memory"]["source_documents"][0]["source_id"] == "document:doc_source"


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

    def fail_route_enrichment(*_args: object, **_kwargs: object) -> list[dict[str, object]]:
        raise AssertionError("route candidates should not be built for filtered-out rows")

    monkeypatch.setattr(ui_server, "route_candidates_for_fact", fail_route_enrichment)

    with running_ui(paths) as (host, port, token):
        status, queue = request_json(
            host,
            port,
            token,
            "GET",
            "/api/queue?kind=proposed_memory&limit=1",
        )

    assert status == 200
    assert queue["total"] == 1
    assert len(queue["items"]) == 1
    assert queue["items"][0]["group"] == "memories"


def test_v2_queue_policy_escalation_uses_human_readable_summary(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()
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
    assert "Fact upsert matched" in item["summary"]
    assert "review level is" in item["summary"]
    assert "matched policy policy_" not in item["summary"]


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

    def count_route_enrichment(*_args: object, **_kwargs: object) -> list[dict[str, object]]:
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


def test_v2_queue_conflict_keys_do_not_collide_with_navigation() -> None:
    source = (Path(__file__).parents[1] / "src/pkm_brain/ui_static/views/queue.js").read_text(
        encoding="utf-8"
    )

    assert '<kbd>1</kbd>keep existing' in source
    assert '<kbd>2</kbd>candidate wins' in source
    assert 'key === "k") doDecision' not in source
    assert 'if (item.group === "conflicts") return {b: "both_true", r: "reject"}' in source


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
    assert undo_status == 200
    assert undo["status"] == "undone"
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
    assert detail["facts_by_page"][0]["facts"][0]["statement"] == "AlphaPay has a browser-visible entity page."


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
    assert body["action"]["evidence_json"]["human_review"]["reason"] == "not supported by quote"
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
            request_json(host, port, token, "POST", "/api/wiki/proposal-packets/facts", {}),
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
    assert correction_status == 200
    assert correction["fact"]["confirmed_by_user"] is True
    assert correction["action"]["action_type"] == "fact_upsert"
    assert correction["action"]["status"] == "applied"
    assert "corrected fact" in correction["review"]["current_markdown"]
    assert revert_status == 200
    assert "original fact" in reverted["review"]["current_markdown"]


def test_cos_control_plane_endpoints_require_auth_and_return_state(tmp_path: Path) -> None:
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
        policy_status, policy = request_json(host, port, token, "GET", "/api/cos/policy")
        actions_status, actions = request_json(host, port, token, "GET", "/api/cos/actions")
        review_status, review = request_json(host, port, token, "GET", "/api/cos/review")
        contracts_status, contracts = request_json(host, port, token, "GET", "/api/cos/contracts")
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
