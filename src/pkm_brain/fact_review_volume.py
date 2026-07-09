from __future__ import annotations

from pathlib import Path
from typing import Any

from .cos_actions import apply_action, propose_action
from .cos_policy import evaluate_policy, promote_policy_for_autonomy
from .db import connection, dumps, loads, rows
from .fact_relations import classify_fact_relation
from .paths import BrainPaths
from .queue_summary import review_queue_summary
from .util import new_id, now_iso
from .wiki_facts import row_to_fact


ACTIVE_QUESTION_STATUSES = {"open", "needs_human"}
SYNTHESIZE_DRAIN_STATUSES = {"needs_human", "proposed"}
FALLBACK_PAGE_HINTS = {"concepts/extracted-facts.md"}
W2A_REPORT_VERSION = "w2a-v1"
W2B_REPORT_VERSION = "w2b-v1"


def reconcile_backlog_w2b_dry_run(
    paths: BrainPaths,
    *,
    sample_limit: int = 10,
) -> dict[str, Any]:
    generated_at = now_iso()
    before = review_queue_summary(paths)
    with connection(paths.sqlite_path) as conn:
        synthesize = synthesize_page_dry_run(conn, sample_limit=sample_limit)
        unrouted = unrouted_inbox_dry_run(conn, sample_limit=sample_limit)
        policy_reasons = policy_reason_dry_run(conn, sample_limit=sample_limit)
    affected_question_ids = sorted(
        {
            *synthesize["affected_question_ids"],
            *unrouted["affected_question_ids"],
        }
    )
    projected_after = projected_queue_after(
        before,
        affected_question_ids,
        new_question_count=int(unrouted["planned_weekly_batch_question_count"]),
    )
    return {
        "status": "dry_run",
        "scope": "w2b",
        "report_version": W2B_REPORT_VERSION,
        "generated_at": generated_at,
        "acceptance_boundary": {
            "requires_approval_before_apply": True,
            "apply_supported_by_this_command": True,
            "reason": "W2b applies policy/backlog changes only after Peter approves this dry-run report.",
        },
        "before": before,
        "projected_after": projected_after,
        "synthesize_page": synthesize,
        "unrouted_inbox_batching": unrouted,
        "human_readable_escalation_reasons": policy_reasons,
        "rollback_paths": [
            "synthesize_page applies through cos_actions with inverse_action_json.delete_synthesis_ids; revert_action can remove derived syntheses after apply.",
            "unrouted Inbox batching will auto-resolve original questions through open_questions.answer with ledger metadata; each generated fact_upsert remains revertable through cos_actions.",
            "policy changes are versioned rows in cos_policy; demotion can activate a later stricter policy version without mutating prior rows.",
        ],
        "next_step": "Review samples, then run the future --apply implementation only after approval.",
    }


def reconcile_backlog_w2b_apply(
    paths: BrainPaths,
    *,
    sample_limit: int = 10,
) -> dict[str, Any]:
    generated_at = now_iso()
    before = review_queue_summary(paths)
    promoted_policy_version = ensure_synthesize_page_l2_policy(paths)
    synthesize = apply_synthesize_page_backlog(paths, sample_limit=sample_limit)
    unrouted = apply_unrouted_inbox_batching(paths, sample_limit=sample_limit)
    after = review_queue_summary(paths)
    return {
        "status": "ok",
        "scope": "w2b",
        "report_version": W2B_REPORT_VERSION,
        "generated_at": generated_at,
        "before": before,
        "after": after,
        "promoted_policy_version": promoted_policy_version,
        "synthesize_page": synthesize,
        "unrouted_inbox_batching": unrouted,
        "rollback_paths": [
            "synthesize_page actions can be reverted with revert_action(action_id), which deletes inserted syntheses via inverse_action_json.",
            "unrouted Inbox fact_upsert actions can be reverted with revert_action(new_action_id); original questions retain old/new action ids in answer metadata.",
            "the W2b policy promotion is append-only; a stricter policy can be promoted after it if audit finds issues.",
        ],
    }


def reconcile_backlog_w2a_dry_run(
    paths: BrainPaths,
    *,
    sample_limit: int = 20,
) -> dict[str, Any]:
    generated_at = now_iso()
    before = review_queue_summary(paths)
    with connection(paths.sqlite_path) as conn:
        items = w2a_candidate_items(conn)
    classified = [classify_w2a_item(item) for item in items]
    by_relation: dict[str, int] = {}
    samples_by_relation: dict[str, list[dict[str, Any]]] = {}
    for item in classified:
        relation = str(item["relation"])
        by_relation[relation] = by_relation.get(relation, 0) + 1
        samples_by_relation.setdefault(relation, [])
        if len(samples_by_relation[relation]) < sample_limit:
            samples_by_relation[relation].append(w2a_sample(item))
    auto_resolvable = [
        item
        for item in classified
        if item["relation"] not in {"contradicts", "unsure"}
    ]
    survivors = [
        item
        for item in classified
        if item["relation"] in {"contradicts", "unsure"}
    ]
    return {
        "status": "dry_run",
        "scope": "w2a",
        "report_version": W2A_REPORT_VERSION,
        "generated_at": generated_at,
        "acceptance_boundary": {
            "requires_approval_before_apply": True,
            "apply_supported_by_this_command": False,
            "reason": "W2a applies classifier-dependent fact resolutions only after Peter reviews this dry-run report.",
        },
        "before": before,
        "candidate_count": len(classified),
        "auto_resolvable_count": len(auto_resolvable),
        "survivor_count": len(survivors),
        "by_relation": dict(sorted(by_relation.items())),
        "samples_by_relation": {
            relation: samples_by_relation[relation]
            for relation in sorted(samples_by_relation)
        },
        "affected_question_ids": sorted(
            str(item["question_id"]) for item in classified if item.get("question_id")
        ),
        "affected_action_ids": sorted(
            str(item["action_id"]) for item in classified if item.get("action_id")
        ),
        "next_step": "Review per-relation samples, then approve W2a --apply separately if the compatible classes look safe.",
    }


def w2a_candidate_items(conn: Any) -> list[dict[str, Any]]:
    if not table_exists(conn, "open_questions"):
        return []
    candidate_rows = rows(
        conn,
        """
        SELECT
          q.*,
          a.id AS linked_action_id,
          a.action_type AS linked_action_type,
          a.target_fact_ids AS action_target_fact_ids,
          a.target_page_paths AS action_target_page_paths,
          a.action_features AS action_features,
          a.evidence_json AS evidence_json,
          a.status AS action_status
        FROM open_questions q
        LEFT JOIN cos_actions a ON a.id = q.action_id
        WHERE q.status IN ('open', 'needs_human')
          AND (
            q.kind = 'fact_conflict_review'
            OR (
              q.kind = 'policy_escalation'
              AND a.action_type = 'fact_upsert'
              AND a.status IN ('needs_human', 'proposed')
            )
          )
        ORDER BY q.created_at, q.id
        """,
    )
    output: list[dict[str, Any]] = []
    for row in candidate_rows:
        action = row_to_action_like(row)
        candidate = candidate_fact_from_question(row, action)
        counterpart_facts = load_counterpart_facts(conn, row, action)
        output.append(
            {
                "question": row,
                "question_id": row["id"],
                "kind": row["kind"],
                "action": action,
                "action_id": row["linked_action_id"] or row["action_id"],
                "candidate": candidate,
                "counterpart_facts": counterpart_facts,
            }
        )
    return output


def row_to_action_like(row: Any) -> dict[str, Any] | None:
    if not row["linked_action_id"]:
        return None
    return {
        "id": row["linked_action_id"],
        "action_type": row["linked_action_type"],
        "target_fact_ids": loads(row["action_target_fact_ids"], []),
        "target_page_paths": loads(row["action_target_page_paths"], []),
        "action_features": loads(row["action_features"], {}),
        "evidence_json": loads(row["evidence_json"], {}),
        "status": row["action_status"],
    }


def load_counterpart_facts(conn: Any, question: Any, action: dict[str, Any] | None) -> list[dict[str, Any]]:
    fact_ids: list[str] = []
    fact_ids.extend(str(item) for item in loads(question["fact_ids"], []) if str(item or "").strip())
    if action is not None:
        fact_ids.extend(str(item) for item in action.get("target_fact_ids") or [] if str(item or "").strip())
        resolver = (action.get("evidence_json") or {}).get("resolver_precheck") or {}
        fact_ids.extend(str(item) for item in resolver.get("counterpart_fact_ids") or [] if str(item or "").strip())
    fact_ids = stable_unique_strings(fact_ids)
    if not fact_ids:
        return []
    placeholders = ",".join("?" for _ in fact_ids)
    return [
        row_to_fact(row)
        for row in conn.execute(
            f"SELECT * FROM facts WHERE id IN ({placeholders})",
            tuple(fact_ids),
        )
    ]


def classify_w2a_item(item: dict[str, Any]) -> dict[str, Any]:
    candidate = item.get("candidate")
    counterpart_facts = item.get("counterpart_facts") or []
    if not isinstance(candidate, dict):
        return {
            **item,
            "relation": "unsure",
            "confidence": 0.0,
            "rationale": "missing candidate fact payload",
            "classifications": [],
        }
    if not counterpart_facts:
        return {
            **item,
            "relation": "unrelated",
            "confidence": 0.7,
            "rationale": "no counterpart facts supplied; routine fact_upsert policy escalation",
            "classifications": [],
        }
    classifications = [
        classify_fact_relation(candidate, counterpart).as_dict()
        for counterpart in counterpart_facts
    ]
    selected = select_w2a_classification(classifications)
    return {
        **item,
        "relation": selected["relation"],
        "confidence": selected["confidence"],
        "rationale": selected["rationale"],
        "classifications": classifications,
    }


def select_w2a_classification(classifications: list[dict[str, Any]]) -> dict[str, Any]:
    if not classifications:
        return {"relation": "unrelated", "confidence": 0.7, "rationale": "no classifications"}
    low_confidence = [item for item in classifications if float(item["confidence"]) < 0.7]
    contradictions = [item for item in classifications if item["relation"] == "contradicts"]
    if contradictions:
        return max(contradictions, key=lambda item: float(item["confidence"]))
    if low_confidence:
        item = min(low_confidence, key=lambda value: float(value["confidence"]))
        return {
            **item,
            "relation": "unsure",
            "rationale": f"classifier confidence below floor: {item['rationale']}",
        }
    precedence = ["updates", "refines", "supports", "duplicate", "complementary", "unrelated"]
    for relation in precedence:
        matching = [item for item in classifications if item["relation"] == relation]
        if matching:
            return max(matching, key=lambda item: float(item["confidence"]))
    return classifications[0]


def w2a_sample(item: dict[str, Any]) -> dict[str, Any]:
    candidate = item.get("candidate") if isinstance(item.get("candidate"), dict) else {}
    return {
        "question_id": item.get("question_id"),
        "action_id": item.get("action_id"),
        "kind": item.get("kind"),
        "relation": item.get("relation"),
        "confidence": item.get("confidence"),
        "rationale": item.get("rationale"),
        "candidate_statement": str(candidate.get("statement") or "")[:220],
        "counterpart_fact_ids": [
            str(fact.get("id") or "") for fact in item.get("counterpart_facts") or []
        ],
        "classification_count": len(item.get("classifications") or []),
    }


def write_reconcile_report(report: dict[str, Any], output: Path) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    import json

    report["report_path"] = str(output)
    output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return output


def ensure_synthesize_page_l2_policy(paths: BrainPaths) -> int | None:
    with connection(paths.sqlite_path) as conn:
        decision = evaluate_policy(
            conn,
            "synthesize_page",
            {
                "risk_tier": "low",
                "truth_mutation": False,
                "reversible": True,
                "affected_fact_count": 1,
            },
        )
        if decision.autonomy_level == "L2" and not decision.critic_required:
            return None
        return promote_policy_for_autonomy(
            conn,
            reason="W2b fact-review-volume: synthesize_page is derived, revertible text and should apply at L2 with sampled audit",
        )


def apply_synthesize_page_backlog(paths: BrainPaths, *, sample_limit: int) -> dict[str, Any]:
    with connection(paths.sqlite_path) as conn:
        candidates = synthesize_page_candidate_rows(conn)
    applied: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    for row in candidates:
        action_id = str(row["action_id"])
        question_id = str(row["question_id"])
        try:
            action = apply_action(paths, action_id, applied_status="applied")
            mark_synthesize_reconciled(paths, action_id, question_id)
            applied.append(
                {
                    "action_id": action_id,
                    "question_id": question_id,
                    "status": action["status"],
                    "target_page_paths": action.get("target_page_paths") or [],
                    "inverse_action_json": action.get("inverse_action_json") or {},
                }
            )
        except Exception as exc:
            failed.append(
                {
                    "action_id": action_id,
                    "question_id": question_id,
                    "error": str(exc)[:500],
                }
            )
    return {
        "applied_count": len(applied),
        "resolved_question_count": len(applied),
        "failed_count": len(failed),
        "applied_samples": applied[:sample_limit],
        "failed": failed[:sample_limit],
    }


def synthesize_page_candidate_rows(conn: Any) -> list[Any]:
    if not table_exists(conn, "open_questions") or not table_exists(conn, "cos_actions"):
        return []
    return rows(
        conn,
        """
        SELECT
          q.id AS question_id,
          a.id AS action_id
        FROM open_questions q
        JOIN cos_actions a ON a.id = q.action_id
        WHERE q.kind = 'policy_escalation'
          AND q.status IN ('open', 'needs_human')
          AND a.action_type = 'synthesize_page'
          AND a.status IN ('needs_human', 'proposed')
        ORDER BY q.created_at, q.id
        """,
    )


def mark_synthesize_reconciled(paths: BrainPaths, action_id: str, question_id: str) -> None:
    timestamp = now_iso()
    with connection(paths.sqlite_path) as conn:
        action = conn.execute(
            "SELECT * FROM cos_actions WHERE id = ?",
            (action_id,),
        ).fetchone()
        features = loads(action["action_features"], {}) if action else {}
        policy_features = {
            **features,
            "risk_tier": action["risk_tier"] if action else features.get("risk_tier"),
            "truth_mutation": False,
            "reversible": True,
        }
        policy_features.pop("eval_gate", None)
        decision = evaluate_policy(conn, "synthesize_page", policy_features)
        evidence = loads(action["evidence_json"], {}) if action else {}
        evidence["w2b_reconciliation"] = {
            "kind": "synthesize_page_auto_apply",
            "question_id": question_id,
            "decided_at": timestamp,
            "removed_eval_gate": "eval_gate" in features,
        }
        conn.execute(
            """
            UPDATE cos_actions
            SET policy_id = ?, policy_version = ?, policy_decision = ?,
                autonomy_level = ?, evidence_json = ?
            WHERE id = ?
            """,
            (
                decision.policy_id,
                decision.policy_version,
                "w2b_synthesize_auto_apply",
                "L2",
                dumps(evidence),
                action_id,
            ),
        )
        conn.execute(
            """
            UPDATE open_questions
            SET status = 'auto_resolved',
                answer = ?,
                answered_at = ?,
                decided_by = 'w2b_reconcile'
            WHERE id = ?
            """,
            (
                dumps(
                    {
                        "resolution": "synthesize_page_auto_apply",
                        "action_id": action_id,
                        "reason": "W2b approved auto-application for derived, revertible syntheses.",
                    }
                ),
                timestamp,
                question_id,
            ),
        )


def apply_unrouted_inbox_batching(paths: BrainPaths, *, sample_limit: int) -> dict[str, Any]:
    with connection(paths.sqlite_path) as conn:
        question_rows = rows(
            conn,
            """
            SELECT *
            FROM open_questions
            WHERE kind = 'unrouted_fact'
              AND status IN ('open', 'needs_human')
            ORDER BY created_at, id
            """,
        )
        action_cache = linked_actions_for_questions(conn, question_rows)
    applied: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    groups: dict[str, dict[str, Any]] = {}
    for question in question_rows:
        action = action_cache.get(str(question["action_id"] or ""))
        candidate = candidate_fact_from_question(question, action)
        if candidate is None:
            skipped.append({"question_id": question["id"], "reason": "missing_candidate_fact"})
            continue
        target = planned_inbox_target(question, candidate)
        inbox_fact = inbox_fact_payload(question, candidate, target)
        try:
            new_action = apply_action(
                paths,
                propose_action(
                    paths,
                    "fact_upsert",
                    action_payload={"fact": inbox_fact},
                    action_features={
                        "human_confirmed": True,
                        "w2b_unrouted_inbox_batch": True,
                        "truth_mutation": True,
                        "reversible": True,
                        "affected_fact_count": 1,
                    },
                    target_fact_ids=[str(inbox_fact.get("id") or "")],
                    target_page_paths=[target["page_hint"]],
                    proposed_by="w2b_reconcile_unrouted_inbox",
                    confidence=float(inbox_fact.get("confidence") or inbox_fact.get("truth_confidence") or 1.0),
                    risk_tier="medium",
                )["id"],
            )
            mark_unrouted_reconciled(
                paths,
                question_id=str(question["id"]),
                old_action_id=str(question["action_id"] or ""),
                new_action_id=str(new_action["id"]),
                target=target,
            )
        except Exception as exc:
            skipped.append(
                {
                    "question_id": question["id"],
                    "old_action_id": question["action_id"],
                    "reason": "apply_failed",
                    "error": str(exc)[:500],
                }
            )
            continue
        group = groups.setdefault(
            target["page_hint"],
            {
                "page_hint": target["page_hint"],
                "section": "Inbox",
                "route_basis": target["route_basis"],
                "question_ids": [],
                "old_action_ids": [],
                "new_action_ids": [],
            },
        )
        group["question_ids"].append(str(question["id"]))
        if question["action_id"]:
            group["old_action_ids"].append(str(question["action_id"]))
        group["new_action_ids"].append(str(new_action["id"]))
        applied.append(
            {
                "question_id": question["id"],
                "old_action_id": question["action_id"],
                "new_action_id": new_action["id"],
                "page_hint": target["page_hint"],
                "section": "Inbox",
            }
        )
    batch_questions = create_unrouted_batch_questions(paths, list(groups.values()))
    return {
        "applied_count": len(applied),
        "resolved_question_count": len(applied),
        "batch_question_count": len(batch_questions),
        "skipped_count": len(skipped),
        "applied_samples": applied[:sample_limit],
        "batch_questions": batch_questions[:sample_limit],
        "skipped_examples": skipped[:sample_limit],
    }


def inbox_fact_payload(question: Any, candidate: dict[str, Any], target: dict[str, str]) -> dict[str, Any]:
    fact = dict(candidate)
    fact.setdefault("id", new_id("fact"))
    fact["page_hint"] = target["page_hint"]
    fact["section_hint"] = "Inbox"
    metadata = fact.get("metadata") if isinstance(fact.get("metadata"), dict) else {}
    fact["metadata"] = {
        **metadata,
        "w2b_unrouted_inbox": {
            "question_id": question["id"],
            "old_page_hint": candidate.get("page_hint"),
            "new_page_hint": target["page_hint"],
            "route_basis": target["route_basis"],
            "filed_at": now_iso(),
        },
    }
    return fact


def mark_unrouted_reconciled(
    paths: BrainPaths,
    *,
    question_id: str,
    old_action_id: str,
    new_action_id: str,
    target: dict[str, str],
) -> None:
    timestamp = now_iso()
    answer = {
        "resolution": "unrouted_inbox_batch",
        "old_action_id": old_action_id,
        "new_action_id": new_action_id,
        "page_hint": target["page_hint"],
        "section": "Inbox",
        "route_basis": target["route_basis"],
    }
    with connection(paths.sqlite_path) as conn:
        conn.execute(
            """
            UPDATE open_questions
            SET status = 'auto_resolved',
                answer = ?,
                answered_at = ?,
                action_id = ?,
                decided_by = 'w2b_reconcile'
            WHERE id = ?
            """,
            (dumps(answer), timestamp, new_action_id, question_id),
        )
        if old_action_id:
            old_row = conn.execute(
                "SELECT * FROM cos_actions WHERE id = ?",
                (old_action_id,),
            ).fetchone()
            if old_row is not None:
                evidence = loads(old_row["evidence_json"], {})
                evidence["w2b_reconciliation"] = answer
                conn.execute(
                    """
                    UPDATE cos_actions
                    SET status = 'rejected',
                        policy_decision = COALESCE(policy_decision, 'w2b_replaced_by_inbox_batch'),
                        evidence_json = ?
                    WHERE id = ?
                    """,
                    (dumps(evidence), old_action_id),
                )


def create_unrouted_batch_questions(
    paths: BrainPaths, groups: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    created: list[dict[str, Any]] = []
    timestamp = now_iso()
    with connection(paths.sqlite_path) as conn:
        for group in sorted(groups, key=lambda item: str(item["page_hint"])):
            if not group["question_ids"]:
                continue
            question_id = new_id("question")
            page_hint = str(group["page_hint"])
            context = {
                "source": "w2b_unrouted_inbox_batch",
                "page_hint": page_hint,
                "section": "Inbox",
                "source_question_ids": group["question_ids"],
                "old_action_ids": group["old_action_ids"],
                "new_action_ids": group["new_action_ids"],
                "route_basis": group["route_basis"],
            }
            conn.execute(
                """
                INSERT INTO open_questions(
                  id, kind, entity_key, page_hint, fact_ids, question, options, status,
                  context, recommended_action, risk_tier, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    question_id,
                    "unrouted_inbox_batch",
                    None,
                    page_hint,
                    dumps([]),
                    f"{len(group['question_ids'])} unrouted facts were filed to the Inbox section on {page_hint}. Sweep or rehome them when convenient.",
                    dumps([]),
                    "needs_human",
                    dumps(context),
                    dumps({"action_type": "review_unrouted_inbox_batch", "page_hint": page_hint}),
                    "low",
                    timestamp,
                ),
            )
            created.append(
                {
                    "question_id": question_id,
                    "page_hint": page_hint,
                    "count": len(group["question_ids"]),
                    "new_action_ids": group["new_action_ids"],
                }
            )
    return created


def synthesize_page_dry_run(conn: Any, *, sample_limit: int) -> dict[str, Any]:
    if not table_exists(conn, "open_questions") or not table_exists(conn, "cos_actions"):
        return empty_synthesize_report("missing open_questions or cos_actions table")
    linked_rows = rows(
        conn,
        """
        SELECT
          q.id AS question_id,
          q.question AS question_text,
          q.page_hint AS question_page_hint,
          q.context AS question_context,
          q.risk_tier AS question_risk_tier,
          q.created_at AS question_created_at,
          a.id AS action_id,
          a.status AS action_status,
          a.target_page_paths AS target_page_paths,
          a.action_features AS action_features,
          a.evidence_json AS evidence_json,
          a.policy_id AS policy_id,
          a.policy_version AS policy_version,
          a.policy_decision AS policy_decision,
          a.autonomy_level AS autonomy_level,
          a.risk_tier AS action_risk_tier,
          a.created_at AS action_created_at
        FROM open_questions q
        JOIN cos_actions a ON a.id = q.action_id
        WHERE q.kind = 'policy_escalation'
          AND q.status IN ('open', 'needs_human')
          AND a.action_type = 'synthesize_page'
          AND a.status IN ('needs_human', 'proposed')
        ORDER BY q.created_at, q.id
        """,
    )
    linked_action_ids = {str(row["action_id"]) for row in linked_rows}
    unlinked_actions = unlinked_synthesize_actions(conn, linked_action_ids, sample_limit=sample_limit)
    samples = [synthesize_sample(row) for row in linked_rows[:sample_limit]]
    action_ids = sorted({*linked_action_ids, *[str(row["id"]) for row in unlinked_actions]})
    question_ids = sorted(str(row["question_id"]) for row in linked_rows)
    return {
        "candidate_action_count": len(action_ids),
        "linked_policy_escalation_question_count": len(question_ids),
        "unlinked_proposed_action_count": len(unlinked_actions),
        "affected_action_ids": action_ids,
        "affected_question_ids": question_ids,
        "planned_policy_change": {
            "action_type": "synthesize_page",
            "target_autonomy": "L2",
            "critic_required": False,
            "audit_sample_rate": 0.25,
            "live_change_in_dry_run": False,
            "rationale": "synthesize_page is derived, hash-stamped, revertible page text; source facts remain untouched.",
        },
        "planned_backlog_action": "auto-apply eligible synthesize_page actions through the existing action ledger after approval.",
        "samples": samples,
        "unlinked_action_samples": [unlinked_synthesize_sample(row) for row in unlinked_actions],
    }


def unlinked_synthesize_actions(conn: Any, linked_action_ids: set[str], *, sample_limit: int) -> list[Any]:
    placeholders = ",".join("?" for _ in linked_action_ids)
    exclude = f"AND id NOT IN ({placeholders})" if linked_action_ids else ""
    return rows(
        conn,
        f"""
        SELECT *
        FROM cos_actions
        WHERE action_type = 'synthesize_page'
          AND status IN ('needs_human', 'proposed')
          {exclude}
        ORDER BY created_at, id
        LIMIT ?
        """,
        (*linked_action_ids, sample_limit) if linked_action_ids else (sample_limit,),
    )


def synthesize_sample(row: Any) -> dict[str, Any]:
    target_pages = loads(row["target_page_paths"], [])
    features = loads(row["action_features"], {})
    evidence = loads(row["evidence_json"], {})
    return {
        "question_id": row["question_id"],
        "action_id": row["action_id"],
        "question": str(row["question_text"] or "")[:240],
        "current_action_status": row["action_status"],
        "policy_id": row["policy_id"],
        "policy_decision": row["policy_decision"],
        "autonomy_level": row["autonomy_level"],
        "risk_tier": row["action_risk_tier"] or row["question_risk_tier"],
        "target_page_paths": target_pages,
        "affected_fact_count": features.get("affected_fact_count"),
        "payload_page_hint": payload_page_hint(evidence),
        "planned_resolution": "apply synthesis through cos_actions; mark linked policy question auto_resolved",
    }


def unlinked_synthesize_sample(row: Any) -> dict[str, Any]:
    evidence = loads(row["evidence_json"], {})
    return {
        "action_id": row["id"],
        "current_action_status": row["status"],
        "target_page_paths": loads(row["target_page_paths"], []),
        "payload_page_hint": payload_page_hint(evidence),
        "planned_resolution": "decide/apply after W2b approval if still eligible",
    }


def payload_page_hint(evidence: dict[str, Any]) -> str | None:
    payload = evidence.get("payload") if isinstance(evidence, dict) else None
    if not isinstance(payload, dict):
        return None
    value = str(payload.get("page_hint") or "").strip()
    return value or None


def unrouted_inbox_dry_run(conn: Any, *, sample_limit: int) -> dict[str, Any]:
    if not table_exists(conn, "open_questions"):
        return empty_unrouted_report("missing open_questions table")
    question_rows = rows(
        conn,
        """
        SELECT *
        FROM open_questions
        WHERE kind = 'unrouted_fact'
          AND status IN ('open', 'needs_human')
        ORDER BY created_at, id
        """,
    )
    action_cache = linked_actions_for_questions(conn, question_rows)
    groups: dict[str, dict[str, Any]] = {}
    skipped: list[dict[str, Any]] = []
    for question in question_rows:
        action = action_cache.get(str(question["action_id"] or ""))
        candidate = candidate_fact_from_question(question, action)
        if not candidate:
            skipped.append({"question_id": question["id"], "reason": "missing_candidate_fact"})
            continue
        target = planned_inbox_target(question, candidate)
        bucket = groups.setdefault(
            target["page_hint"],
            {
                "page_hint": target["page_hint"],
                "section": "Inbox",
                "route_basis": target["route_basis"],
                "question_ids": [],
                "action_ids": [],
                "sample_statements": [],
            },
        )
        bucket["question_ids"].append(str(question["id"]))
        action_id = str(question["action_id"] or "").strip()
        if action_id:
            bucket["action_ids"].append(action_id)
        if len(bucket["sample_statements"]) < 3:
            bucket["sample_statements"].append(str(candidate.get("statement") or "")[:220])
    group_list = sorted(groups.values(), key=lambda item: (-len(item["question_ids"]), item["page_hint"]))
    affected_question_ids = sorted(str(row["id"]) for row in question_rows)
    return {
        "candidate_question_count": len(question_rows),
        "affected_question_ids": affected_question_ids,
        "planned_group_count": len(group_list),
        "planned_weekly_batch_question_count": len(group_list),
        "planned_action": "append candidate facts to each target page's Inbox section and replace individual unrouted prompts with one weekly batch question per page after approval.",
        "live_change_in_dry_run": False,
        "groups": [
            {
                **group,
                "count": len(group["question_ids"]),
                "question_ids": group["question_ids"][:sample_limit],
                "action_ids": sorted(set(group["action_ids"]))[:sample_limit],
            }
            for group in group_list[:sample_limit]
        ],
        "skipped_count": len(skipped),
        "skipped_examples": skipped[:sample_limit],
    }


def linked_actions_for_questions(conn: Any, question_rows: list[Any]) -> dict[str, Any]:
    if not table_exists(conn, "cos_actions"):
        return {}
    action_ids = sorted({str(row["action_id"] or "") for row in question_rows if str(row["action_id"] or "").strip()})
    if not action_ids:
        return {}
    placeholders = ",".join("?" for _ in action_ids)
    return {
        str(row["id"]): row
        for row in rows(
            conn,
            f"SELECT * FROM cos_actions WHERE id IN ({placeholders})",
            tuple(action_ids),
        )
    }


def candidate_fact_from_question(question: Any, action: Any | None) -> dict[str, Any] | None:
    if action is not None:
        evidence_value = action["evidence_json"]
        evidence = evidence_value if isinstance(evidence_value, dict) else loads(evidence_value, {})
        payload = evidence.get("payload") if isinstance(evidence, dict) else None
        if isinstance(payload, dict):
            fact = payload.get("fact")
            if isinstance(fact, dict):
                return fact
            if payload.get("statement"):
                return payload
    context = loads(question["context"], {})
    for key in ("fact", "candidate", "candidate_fact"):
        fact = context.get(key) if isinstance(context, dict) else None
        if isinstance(fact, dict):
            return fact
    options = loads(question["options"], [])
    for option in options:
        if isinstance(option, dict) and option.get("statement"):
            return option
    return None


def planned_inbox_target(question: Any, candidate: dict[str, Any]) -> dict[str, str]:
    routing = {}
    metadata = candidate.get("metadata")
    if isinstance(metadata, dict) and isinstance(metadata.get("routing"), dict):
        routing = metadata["routing"]
    for key in ("snapped_page_hint", "resolved_page_hint", "original_page_hint", "normalized_page_hint"):
        value = normalized_page_hint(routing.get(key))
        if value and value not in FALLBACK_PAGE_HINTS:
            return {"page_hint": value, "route_basis": f"candidate.metadata.routing.{key}"}
    for key in ("page_hint",):
        value = normalized_page_hint(candidate.get(key))
        if value and value not in FALLBACK_PAGE_HINTS:
            return {"page_hint": value, "route_basis": f"candidate.{key}"}
    value = normalized_page_hint(question["page_hint"])
    if value and value not in FALLBACK_PAGE_HINTS:
        return {"page_hint": value, "route_basis": "question.page_hint"}
    entity_key = str(candidate.get("entity_key") or question["entity_key"] or "").strip()
    if entity_key:
        return {
            "page_hint": f"inbox/{safe_page_slug(entity_key)}.md",
            "route_basis": "entity_key_fallback",
        }
    return {"page_hint": "inbox/unrouted-facts.md", "route_basis": "global_fallback"}


def normalized_page_hint(value: Any) -> str:
    text = str(value or "").strip().lstrip("/")
    if not text:
        return ""
    return text if text.endswith(".md") else f"{text}.md"


def safe_page_slug(value: str) -> str:
    import re

    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "unrouted-facts"


def stable_unique_strings(values: list[str]) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        output.append(text)
    return output


def policy_reason_dry_run(conn: Any, *, sample_limit: int) -> dict[str, Any]:
    if not table_exists(conn, "open_questions"):
        return {"active_policy_escalation_count": 0, "opaque_reason_count": 0, "samples": []}
    question_rows = rows(
        conn,
        """
        SELECT q.id, q.question, q.action_id, a.action_type, a.policy_id, a.autonomy_level, a.action_features
        FROM open_questions q
        LEFT JOIN cos_actions a ON a.id = q.action_id
        WHERE q.kind = 'policy_escalation'
          AND q.status IN ('open', 'needs_human')
        ORDER BY q.created_at, q.id
        """,
    )
    opaque = [
        row
        for row in question_rows
        if str(row["question"] or "").startswith("matched policy ")
        or "matched policy policy_" in str(row["question"] or "")
    ]
    return {
        "active_policy_escalation_count": len(question_rows),
        "opaque_reason_count": len(opaque),
        "samples": [
            {
                "question_id": row["id"],
                "action_id": row["action_id"],
                "action_type": row["action_type"],
                "current_question": str(row["question"] or "")[:220],
                "policy_id": row["policy_id"],
                "autonomy_level": row["autonomy_level"],
                "planned_reason_shape": planned_policy_reason_shape(
                    str(row["action_type"] or ""),
                    str(row["policy_id"] or ""),
                    str(row["autonomy_level"] or ""),
                    loads(row["action_features"], {}),
                ),
            }
            for row in opaque[:sample_limit]
        ],
    }


def planned_policy_reason_shape(
    action_type: str, policy_id: str, autonomy_level: str, features: dict[str, Any]
) -> str:
    risk = str(features.get("risk_tier") or "").strip()
    if action_type == "synthesize_page":
        return (
            f"Synthesis is derived, revertible page text; policy {policy_id} routes "
            f"{risk or 'this'} synthesis to {autonomy_level or 'review'}."
        )
    if action_type == "fact_upsert":
        return (
            f"Fact upsert needs review because the current policy {policy_id} routes "
            f"{risk or 'this'} fact evidence to {autonomy_level or 'review'}."
        )
    return (
        f"{action_type or 'Action'} needs review because policy {policy_id or 'unknown'} "
        f"routes it to {autonomy_level or 'human review'}."
    )


def projected_queue_after(
    before: dict[str, Any], affected_question_ids: list[str], *, new_question_count: int
) -> dict[str, Any]:
    raw = dict(before.get("raw") or {})
    by_kind = dict(before.get("by_kind") or {})
    removable = len(set(affected_question_ids))
    after_total = max(0, int(before.get("total") or 0) - removable + int(new_question_count))
    return {
        "total_if_w2b_applied": after_total,
        "question_count_removed": removable,
        "new_weekly_batch_question_count": int(new_question_count),
        "net_question_delta": int(new_question_count) - removable,
        "note": "Projection removes active synthesize policy-escalation and unrouted questions, then adds one unrouted Inbox batch question per affected page; audit/proposed action counts may shift during apply.",
        "before_raw": raw,
        "before_by_kind": by_kind,
    }


def table_exists(conn: Any, table: str) -> bool:
    return bool(
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type IN ('table', 'view') AND name = ?",
            (table,),
        ).fetchone()
    )


def empty_synthesize_report(reason: str) -> dict[str, Any]:
    return {
        "candidate_action_count": 0,
        "linked_policy_escalation_question_count": 0,
        "unlinked_proposed_action_count": 0,
        "affected_action_ids": [],
        "affected_question_ids": [],
        "samples": [],
        "reason": reason,
    }


def empty_unrouted_report(reason: str) -> dict[str, Any]:
    return {
        "candidate_question_count": 0,
        "affected_question_ids": [],
        "planned_group_count": 0,
        "planned_weekly_batch_question_count": 0,
        "groups": [],
        "skipped_count": 0,
        "skipped_examples": [],
        "reason": reason,
    }
