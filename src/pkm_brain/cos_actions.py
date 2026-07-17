from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

from .critic_context import critic_named_entity_context
from .cos_policy import PolicyDecision, classify_action_risk, evaluate_policy
from .db import connection, dumps, loads
from .entities import (
    DEFAULT_ADMIT_KINDS,
    EntityResolution,
    normalize_admit_kinds,
    normalize_entity_name,
    normalize_entity_type,
    normalize_mention_kind,
    replace_fact_entity_links,
    resolve_entity,
)
from .fact_event_integrity import (
    event_time_for_resolved_primary_event,
    reject_incompatible_fact_merge,
)
from .fact_records import (
    merge_fact_inverse,
    revise_stored_fact,
    stored_fact_entity_links,
    write_versioned_fact,
)
from .llm import (
    LLMProviderError,
    LLMProvider,
    complete_json,
    cos_role_provider_configured,
    get_cos_action_provider,
    load_cos_llm_config,
)
from .paths import BrainPaths
from .source_evidence import evidence_units_for_text, resolve_evidence_unit_ids
from .util import new_id, now_iso


APPLIED_STATUSES = {"applied", "auto_applied"}
OPEN_ACTION_STATUSES = {"proposed", "needs_human"}
CRITIC_DISAGREEMENT_MODES = {"needs_human", "reject"}
MAX_CRITIC_REPAIR_EVIDENCE_UNITS = 5
CRITIC_CONTEXT_RADIUS = 4
CRITIC_SCHEMA = {
    "type": "object",
    "required": ["decision", "rationale"],
    "properties": {
        "decision": {
            "type": "string",
            "enum": ["agree", "evidence_incomplete", "disagree"],
        },
        "rationale": {"type": "string"},
        "repaired_evidence_unit_ids": {
            "type": "array",
            "items": {"type": "string"},
        },
    },
}
ACTION_TYPE_SPECS: dict[str, dict[str, Any]] = {
    "fact_upsert": {
        "class": "ledger_mutation",
        "implemented": True,
        "inverse_keys": ["restore_fact", "delete_fact_ids"],
        "projection": "rebuild_fact_retrieval_index; managed page rendering remains a caller-triggered projection",
    },
    "rehome_fact": {
        "class": "ledger_mutation",
        "implemented": True,
        "inverse_keys": ["restore_fact_routing"],
        "projection": "rebuild_fact_retrieval_index; affected managed pages must be reprojected by caller",
    },
    "edit_contract": {
        "class": "ledger_mutation",
        "implemented": True,
        "inverse_keys": ["restore_contract", "delete_contract_ids"],
        "projection": "contract validation/page projection remains caller-triggered",
    },
    "synthesize_page": {
        "class": "derived_mutation",
        "implemented": True,
        "inverse_keys": ["delete_synthesis_ids"],
        "projection": "derived synthesis only; never source evidence",
    },
    "canonicalize_page": {
        "class": "deterministic_projection",
        "implemented": True,
        "inverse_keys": ["noop"],
        "projection": "no-op action marker for deterministic canonicalization",
    },
    "fact_merge": {
        "class": "ledger_mutation",
        "implemented": True,
        "inverse_keys": ["restore_facts"],
        "projection": "rebuild_fact_retrieval_index; affected managed pages must be reprojected by caller",
    },
    "fact_supersede": {
        "class": "ledger_mutation",
        "implemented": True,
        "inverse_keys": ["restore_facts"],
        "projection": "rebuild_fact_retrieval_index; affected managed pages must be reprojected by caller",
    },
    "resolve_conflict": {
        "class": "ledger_mutation",
        "implemented": True,
        "inverse_keys": ["restore_facts"],
        "projection": "rebuild_fact_retrieval_index; affected managed pages must be reprojected by caller",
    },
    "display_contested": {
        "class": "ledger_mutation",
        "implemented": True,
        "inverse_keys": ["restore_facts", "delete_question_ids"],
        "projection": "rebuild_fact_retrieval_index; open question residue references this action",
    },
    "entity_merge": {
        "class": "ledger_mutation",
        "implemented": True,
        "inverse_keys": [
            "restore_entities",
            "restore_fact_entity_links",
            "restore_fact_entity_denorms",
        ],
        "projection": "rebuild_fact_retrieval_index; affected entity-scoped facts must be reprojected",
    },
    "entity_split": {
        "class": "ledger_mutation",
        "implemented": True,
        "inverse_keys": [
            "restore_entities",
            "restore_fact_entity_links",
            "restore_fact_entity_denorms",
        ],
        "projection": "rebuild_fact_retrieval_index; restores prior entity topology links",
    },
    "page_merge": {
        "class": "ledger_mutation",
        "implemented": True,
        "inverse_keys": ["restore_facts", "restore_contracts", "restore_syntheses"],
        "projection": "moves source facts to a destination page; affected pages must be reprojected",
    },
    "page_split": {
        "class": "ledger_mutation",
        "implemented": True,
        "inverse_keys": ["restore_facts", "restore_syntheses"],
        "projection": "moves section-scoped facts to generated child pages; affected pages must be reprojected",
    },
    "rename_page": {
        "class": "ledger_mutation",
        "implemented": True,
        "inverse_keys": ["restore_facts", "restore_contracts", "restore_syntheses"],
        "projection": "renames fact routing, contracts, syntheses, and indexed wiki page path",
    },
    "archive_page": {
        "class": "deterministic_projection",
        "implemented": True,
        "inverse_keys": ["noop"],
        "projection": "records a managed-page archive projection; page markdown remains rebuildable",
    },
    "revert_page_snapshot": {
        "class": "deterministic_projection",
        "implemented": True,
        "inverse_keys": ["noop"],
        "projection": "records a guarded page snapshot revert projection",
    },
}


def propose_action(
    paths: BrainPaths,
    action_type: str,
    *,
    action_payload: dict[str, Any] | None = None,
    action_features: dict[str, Any] | None = None,
    run_id: str | None = None,
    target_fact_ids: list[str] | None = None,
    target_page_paths: list[str] | None = None,
    target_contract_ids: list[str] | None = None,
    proposed_by: str = "system",
    confidence: float | None = None,
    risk_tier: str | None = None,
    evidence: dict[str, Any] | None = None,
    decide: bool = False,
) -> dict[str, Any]:
    validate_action_type(action_type)
    action_id = new_id("cosact")
    evidence_json = dict(evidence or {})
    if action_payload is not None:
        evidence_json["payload"] = action_payload
    features = dict(action_features or {})
    if confidence is not None:
        features.setdefault("confidence", float(confidence))
    features.setdefault("target_fact_ids", target_fact_ids or [])
    features.setdefault("target_page_paths", target_page_paths or [])
    features.setdefault("target_contract_ids", target_contract_ids or [])
    resolved_risk_tier = classify_action_risk(
        action_type, features, explicit_risk_tier=risk_tier
    )
    features.setdefault("risk_tier", resolved_risk_tier)
    created_at = now_iso()
    candidate_key = str(features.get("candidate_key") or "").strip()
    existing_action_id: str | None = None
    with connection(paths.sqlite_path) as conn:
        existing = (
            open_action_for_candidate_key(conn, action_type, candidate_key)
            if candidate_key
            else None
        )
        if existing is not None:
            existing_action_id = str(existing["id"])
        elif run_id:
            conn.execute(
                """
                INSERT OR IGNORE INTO wiki_curation_runs(
                  id, source_packet_id, group_by, status, summary, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    None,
                    "cos_action",
                    "running",
                    dumps({"created_by": "propose_action"}),
                    created_at,
                ),
            )
        if existing_action_id is None:
            conn.execute(
                """
                INSERT INTO cos_actions(
                  id, run_id, action_type, status, target_fact_ids, target_page_paths,
                  target_contract_ids, action_features, proposed_by, confidence,
                  risk_tier, evidence_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    action_id,
                    run_id,
                    action_type,
                    "proposed",
                    dumps(target_fact_ids or []),
                    dumps(target_page_paths or []),
                    dumps(target_contract_ids or []),
                    dumps(features),
                    proposed_by,
                    confidence,
                    resolved_risk_tier,
                    dumps(evidence_json),
                    created_at,
                ),
            )
    if existing_action_id is not None:
        existing_action = get_action(paths, existing_action_id)
        if decide and existing_action["status"] == "proposed":
            return decide_action(paths, existing_action_id)
        return existing_action
    if decide:
        return decide_action(paths, action_id)
    return get_action(paths, action_id)


def open_action_for_candidate_key(
    conn: Any, action_type: str, candidate_key: str
) -> dict[str, Any] | None:
    if not candidate_key:
        return None
    rows = conn.execute(
        """
        SELECT *
        FROM cos_actions
        WHERE action_type = ?
          AND status IN ('proposed', 'needs_human')
        ORDER BY created_at, id
        """,
        (action_type,),
    )
    for row in rows:
        action = row_to_action(row)
        if action_candidate_key(action) == candidate_key:
            return action
    return None


def action_candidate_key(action: dict[str, Any]) -> str:
    features = action.get("action_features")
    if isinstance(features, dict):
        key = str(features.get("candidate_key") or "").strip()
        if key:
            return key
    payload = action_payload(action)
    if isinstance(payload, dict):
        candidate = payload.get("candidate")
        if isinstance(candidate, dict):
            return str(candidate.get("candidate_key") or "").strip()
        return str(payload.get("candidate_key") or "").strip()
    return ""


def retire_open_candidate_siblings(
    conn: Any, action: dict[str, Any], *, reason: str
) -> list[str]:
    candidate_key = action_candidate_key(action)
    if not candidate_key:
        return []
    sibling_ids: list[str] = []
    rows = conn.execute(
        """
        SELECT *
        FROM cos_actions
        WHERE action_type = ?
          AND status IN ('proposed', 'needs_human')
          AND id != ?
        ORDER BY created_at, id
        """,
        (action["action_type"], action["id"]),
    )
    for row in rows:
        sibling = row_to_action(row)
        if action_candidate_key(sibling) != candidate_key:
            continue
        sibling_ids.append(str(sibling["id"]))
        evidence = dict(sibling.get("evidence_json") or {})
        evidence["candidate_superseded"] = {
            "by_action_id": action["id"],
            "candidate_key": candidate_key,
            "reason": reason,
            "at": now_iso(),
        }
        conn.execute(
            "UPDATE cos_actions SET status = 'dismissed', evidence_json = ? WHERE id = ?",
            (dumps(evidence), sibling["id"]),
        )
    if sibling_ids:
        placeholders = ",".join("?" for _ in sibling_ids)
        answer = dumps(
            {
                "decision": "obsolete",
                "reason": reason,
                "superseded_by_action_id": action["id"],
                "candidate_key": candidate_key,
            }
        )
        conn.execute(
            f"""
            UPDATE open_questions
            SET status = 'dismissed', answer = ?, answered_at = ?,
                decided_by = 'candidate_deduplication'
            WHERE action_id IN ({placeholders})
              AND status IN ('open', 'needs_human')
            """,
            [answer, now_iso(), *sibling_ids],
        )
    return sibling_ids


def decide_action(
    paths: BrainPaths,
    action_id: str,
    *,
    critic_by: str | None = None,
    critic_decision: str | None = None,
    critic_rationale: str | None = None,
    precomputed_critic_review: dict[str, Any] | None = None,
    critic_llm_provider: LLMProvider | None = None,
    critic_provider: str | None = None,
    critic_timeout_seconds: int | None = None,
    critic_disagreement_mode: str = "needs_human",
) -> dict[str, Any]:
    if critic_disagreement_mode not in CRITIC_DISAGREEMENT_MODES:
        raise ValueError(
            f"critic_disagreement_mode must be one of {sorted(CRITIC_DISAGREEMENT_MODES)}"
        )
    with connection(paths.sqlite_path) as conn:
        action = load_action(conn, action_id)
        decision = evaluate_policy(
            conn, action["action_type"], action.get("action_features") or {}
        )
    if decision.critic_required and critic_decision is None:
        review = (
            normalize_precomputed_critic_review(precomputed_critic_review)
            if precomputed_critic_review is not None
            else critic_review(
                paths,
                action,
                decision,
                llm_provider=critic_llm_provider,
                provider=critic_provider,
                timeout_seconds=critic_timeout_seconds,
            )
        )
        if review["decision"] == "evidence_incomplete":
            initial_review = dict(review)
            repair = repair_fact_action_evidence(paths, action, review)
            if repair["status"] == "repaired":
                review = critic_review(
                    paths,
                    action,
                    decision,
                    llm_provider=critic_llm_provider,
                    provider=critic_provider,
                    timeout_seconds=critic_timeout_seconds,
                )
                if review["decision"] == "evidence_incomplete":
                    review = {
                        "critic_by": review["critic_by"],
                        "decision": "disagree",
                        "rationale": (
                            "Citation remained incomplete after one bounded evidence repair: "
                            f"{review.get('rationale') or ''}"
                        )[:1000],
                    }
            else:
                review = {
                    "critic_by": initial_review["critic_by"],
                    "decision": "disagree",
                    "rationale": (
                        "Critic identified incomplete evidence, but no valid bounded citation "
                        f"repair was available: {repair.get('reason') or 'unknown reason'}"
                    )[:1000],
                }
            action["evidence_json"] = evidence_with_critic_repair(
                action,
                initial_review=initial_review,
                repair=repair,
                final_review=review,
            )
        critic_by = review["critic_by"]
        critic_decision = review["decision"]
        action["evidence_json"] = evidence_with_critic_review(action, review)
    elif decision.critic_required and critic_decision is not None:
        action["evidence_json"] = evidence_with_critic_review(
            action,
            {
                "critic_by": critic_by or "critic:provided",
                "decision": critic_decision,
                "rationale": critic_rationale or "",
            },
        )
    with connection(paths.sqlite_path) as conn:
        conn.execute(
            """
            UPDATE cos_actions
            SET policy_id = ?, policy_version = ?, policy_decision = ?,
                autonomy_level = ?, critic_by = COALESCE(?, critic_by),
                critic_decision = COALESCE(?, critic_decision),
                evidence_json = ?
            WHERE id = ?
            """,
            (
                decision.policy_id,
                decision.policy_version,
                decision.policy_decision,
                decision.autonomy_level,
                critic_by,
                critic_decision,
                dumps(action.get("evidence_json") or {}),
                action_id,
            ),
        )
    if decision.autonomy_level == "L0":
        return apply_decided_action(
            paths, action_id, applied_status="auto_applied", action=action
        )
    if decision.autonomy_level == "L1":
        if decision.critic_required and critic_decision != "agree":
            handle_critic_disagreement(
                paths, action_id, decision, critic_disagreement_mode
            )
            return get_action(paths, action_id)
        return apply_decided_action(
            paths, action_id, applied_status="auto_applied", action=action
        )
    if decision.autonomy_level == "L2":
        if decision.critic_required and critic_decision != "agree":
            handle_critic_disagreement(
                paths, action_id, decision, critic_disagreement_mode
            )
            return get_action(paths, action_id)
        return apply_decided_action(
            paths, action_id, applied_status="applied", action=action
        )
    mark_needs_human(
        paths, action_id, decision, decision.reason or "requires human decision"
    )
    return get_action(paths, action_id)


def apply_decided_action(
    paths: BrainPaths,
    action_id: str,
    *,
    applied_status: str,
    action: dict[str, Any],
) -> dict[str, Any]:
    try:
        return apply_action(paths, action_id, applied_status=applied_status)
    except LLMProviderError:
        if action.get("action_type") != "fact_upsert":
            raise
        return apply_action(
            paths,
            action_id,
            applied_status=applied_status,
            allow_llm_entity_resolution=False,
        )


def critic_review(
    paths: BrainPaths,
    action: dict[str, Any],
    decision: PolicyDecision,
    *,
    llm_provider: LLMProvider | None = None,
    provider: str | None = None,
    timeout_seconds: int | None = None,
) -> dict[str, Any]:
    if not cos_role_provider_configured(
        paths, "critic", llm_provider=llm_provider, provider=provider
    ):
        return {
            "critic_by": "critic:unconfigured",
            "decision": "unavailable",
            "rationale": "No CoS LLM provider configured for critic role",
        }
    active_provider = get_cos_action_provider(
        paths,
        "critic",
        action,
        provider=provider,
        llm_provider=llm_provider,
        stage="evaluation",
    )
    if active_provider is None:
        return {
            "critic_by": "critic:unconfigured",
            "decision": "unavailable",
            "rationale": "No CoS LLM provider configured for critic role",
        }
    previous_timeout = getattr(active_provider, "timeout", None)
    if (
        timeout_seconds is not None
        and timeout_seconds > 0
        and hasattr(active_provider, "timeout")
    ):
        setattr(active_provider, "timeout", int(timeout_seconds))
    try:
        parsed = complete_json(
            critic_prompt(
                action,
                decision,
                source_context=critic_fact_source_context(paths, action),
            ),
            schema=CRITIC_SCHEMA,
            role="critic",
            provider=provider,
            llm_provider=active_provider,
            paths=paths,
        )
    except LLMProviderError as exc:
        return {
            "critic_by": critic_provider_label(active_provider),
            "decision": "unavailable",
            "rationale": str(exc)[:1000],
        }
    finally:
        if previous_timeout is not None and hasattr(active_provider, "timeout"):
            setattr(active_provider, "timeout", previous_timeout)
    review: dict[str, Any] = {
        "critic_by": critic_provider_label(active_provider),
        "decision": normalize_critic_decision(parsed.get("decision")),
        "rationale": str(parsed.get("rationale") or "")[:1000],
    }
    repair_unit_ids = stable_critic_repair_unit_ids(
        parsed.get("repaired_evidence_unit_ids")
    )
    if repair_unit_ids:
        review["repaired_evidence_unit_ids"] = repair_unit_ids
    return review


def critic_prompt(
    action: dict[str, Any],
    decision: PolicyDecision,
    *,
    source_context: dict[str, Any] | None = None,
) -> str:
    action_card = {
        "id": action.get("id"),
        "action_type": action.get("action_type"),
        "risk_tier": action.get("risk_tier"),
        "confidence": action.get("confidence"),
        "features": action.get("action_features") or {},
        "targets": {
            "fact_ids": action.get("target_fact_ids") or [],
            "page_paths": action.get("target_page_paths") or [],
            "contract_ids": action.get("target_contract_ids") or [],
        },
        "payload": action_payload(action),
        "proposed_by": action.get("proposed_by"),
    }
    policy_card = {
        "policy_id": decision.policy_id,
        "policy_version": decision.policy_version,
        "autonomy_level": decision.autonomy_level,
        "reason": decision.reason,
    }
    source_context_card = source_context or {"available": False}
    return (
        "Review this Chief-of-Staff action before autonomous application. "
        "For fact_upsert actions, answer only the narrow support question: is the proposed statement "
        "directly entailed by the cited evidence in the payload, with negation, uncertainty, entity, "
        "quantity, attribution, and any optional predicate-validity or event-time fields preserved? Return "
        "'agree' when the cited evidence directly supports the statement, even if the fact is mundane or "
        "you would not have written it yourself. Missing temporal enrichment is valid and must not count against "
        "the fact. Require direct support for any returned valid_from/valid_to; source, meeting, capture, ingestion, "
        "and job time are not predicate validity unless the claim explicitly connects them. A plan's target or "
        "deadline is not valid_from. event_time is allowed only for the primary event entity, with kind actual or "
        "planned and source-supported start/end bounds. Trusted structured_metadata projections may use exact "
        "event_started_at/event_ended_at frontmatter as direct support for a named meeting occurrence. "
        "Return 'evidence_incomplete' only when the statement "
        "is directly supported by the "
        "repairable context units from the same chunk but the current citation omitted necessary units; "
        "then return up to 5 repaired_evidence_unit_ids using only ids in repairable_units. You may return "
        "only omitted units; deterministic repair unions them with the current citation. Return 'disagree' "
        "when the statement remains unsupported, over-broad, misattributed, or contradictory after "
        "considering context. Document titles and participant lists are context, not proof of a substantive "
        "claim. Speaker identity context may establish attribution but cannot establish the claim itself. "
        "Named-entity attribution context may clarify spelling or an explicitly linked pronoun, but the mere "
        "presence of a name elsewhere in the same chunk or document does not prove that the cited predicate "
        "belongs to that entity. If a necessary attribution unit is available in repairable_units, return "
        "'evidence_incomplete'; otherwise return 'disagree' for an unsupported named attribution. "
        "For non-fact actions, require the payload, targets, policy, and risk features to support safe application. "
        "The Policy card is the matched authorization record; do not require the action payload to repeat policy fields or invent another requirement. Judge whether the evidence and targets satisfy it. Do not rewrite the action.\n\n"
        f"Action:\n{action_card}\n\nSource context:\n{source_context_card}\n\nPolicy:\n{policy_card}"
    )


def normalize_critic_decision(value: Any) -> str:
    decision = str(value or "").strip().lower()
    if decision in {"agree", "approve", "approved", "pass", "passed", "ok", "safe"}:
        return "agree"
    if decision in {
        "disagree",
        "reject",
        "rejected",
        "block",
        "blocked",
        "fail",
        "failed",
        "unsafe",
    }:
        return "disagree"
    if decision in {
        "evidence_incomplete",
        "incomplete_evidence",
        "citation_incomplete",
        "repair_evidence",
    }:
        return "evidence_incomplete"
    return "unavailable"


def normalize_precomputed_critic_review(value: dict[str, Any]) -> dict[str, Any]:
    review: dict[str, Any] = {
        "critic_by": str(value.get("critic_by") or "critic:provided"),
        "decision": normalize_critic_decision(value.get("decision")),
        "rationale": str(value.get("rationale") or "")[:1000],
    }
    repair_unit_ids = stable_critic_repair_unit_ids(
        value.get("repaired_evidence_unit_ids")
    )
    if repair_unit_ids:
        review["repaired_evidence_unit_ids"] = repair_unit_ids
    return review


def stable_critic_repair_unit_ids(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    output: list[str] = []
    for item in value:
        unit_id = str(item or "").strip()
        if not unit_id or unit_id in output:
            continue
        output.append(unit_id)
        if len(output) >= MAX_CRITIC_REPAIR_EVIDENCE_UNITS:
            break
    return output


def stable_unique_strings(values: list[Any]) -> list[str]:
    output: list[str] = []
    for item in values:
        value = str(item or "").strip()
        if value and value not in output:
            output.append(value)
    return output


def critic_fact_source_context(
    paths: BrainPaths, action: dict[str, Any]
) -> dict[str, Any] | None:
    if action.get("action_type") != "fact_upsert":
        return None
    payload = action_payload(action)
    fact = payload.get("fact") if isinstance(payload.get("fact"), dict) else None
    if fact is None:
        return {"available": False, "reason": "fact payload missing"}
    chunk_id = critic_fact_chunk_id(fact)
    if not chunk_id:
        return {"available": False, "reason": "fact evidence chunk missing"}
    with connection(paths.sqlite_path) as conn:
        row = conn.execute(
            """
            SELECT c.id AS chunk_id, c.document_id, c.chunk_index, c.text,
                   d.title, d.source_type
            FROM chunks c
            JOIN documents d ON d.id = c.document_id
            WHERE c.id = ?
            """,
            (chunk_id,),
        ).fetchone()
        if row is None:
            return {"available": False, "reason": "evidence chunk not found"}
        document_chunks = list(
            conn.execute(
                """
                SELECT id, chunk_index, text
                FROM chunks
                WHERE document_id = ?
                ORDER BY chunk_index
                """,
                (row["document_id"],),
            )
        )
    units = evidence_units_for_text(str(row["text"] or ""))
    units_by_id = {str(unit["unit_id"]): unit for unit in units}
    cited_unit_ids = critic_fact_evidence_unit_ids(fact)
    cited_units = [
        units_by_id[unit_id] for unit_id in cited_unit_ids if unit_id in units_by_id
    ]
    cited_indexes = [int(unit["unit_index"]) for unit in cited_units]
    if cited_indexes:
        context_start = max(0, min(cited_indexes) - CRITIC_CONTEXT_RADIUS)
        context_end = min(len(units), max(cited_indexes) + CRITIC_CONTEXT_RADIUS + 1)
    else:
        context_start = 0
        context_end = min(len(units), CRITIC_CONTEXT_RADIUS * 2 + 1)
    repairable_units = [
        critic_unit_card(unit, cited_unit_ids=cited_unit_ids)
        for unit in units[context_start:context_end]
    ]
    relevant_speakers = {
        str(unit.get("speaker") or "") for unit in cited_units if unit.get("speaker")
    }
    return {
        "available": True,
        "document": {
            "document_id": str(row["document_id"]),
            "title": str(row["title"] or ""),
            "source_type": str(row["source_type"] or ""),
        },
        "repairable_chunk_id": chunk_id,
        "currently_cited_unit_ids": cited_unit_ids,
        "repairable_units": repairable_units,
        "speaker_identity_context": critic_speaker_identity_context(
            document_chunks, relevant_speakers
        ),
        "named_entity_attribution_context": critic_named_entity_context(
            document_chunks, fact
        ),
        "known_participants": critic_known_participants(document_chunks),
    }


def critic_fact_chunk_id(fact: dict[str, Any]) -> str:
    metadata = fact.get("metadata") if isinstance(fact.get("metadata"), dict) else {}
    evidence_units = metadata.get("evidence_units")
    if isinstance(evidence_units, list):
        for unit in evidence_units:
            if isinstance(unit, dict) and str(unit.get("chunk_id") or "").strip():
                return str(unit["chunk_id"]).strip()
    spans = fact.get("source_spans")
    if isinstance(spans, list):
        for span in spans:
            if isinstance(span, dict) and str(span.get("chunk_id") or "").strip():
                return str(span["chunk_id"]).strip()
    return ""


def critic_fact_evidence_unit_ids(fact: dict[str, Any]) -> list[str]:
    direct = fact.get("evidence_unit_ids")
    if isinstance(direct, list):
        unit_ids = stable_critic_repair_unit_ids(direct)
        if unit_ids:
            return unit_ids
    metadata = fact.get("metadata") if isinstance(fact.get("metadata"), dict) else {}
    evidence_units = metadata.get("evidence_units")
    if not isinstance(evidence_units, list):
        return []
    return stable_critic_repair_unit_ids(
        [unit.get("unit_id") for unit in evidence_units if isinstance(unit, dict)]
    )


def critic_unit_card(
    unit: dict[str, Any], *, cited_unit_ids: list[str]
) -> dict[str, Any]:
    return {
        "unit_id": str(unit["unit_id"]),
        "text": str(unit["text"]),
        "cited": str(unit["unit_id"]) in cited_unit_ids,
        **({"speaker": str(unit["speaker"])} if unit.get("speaker") else {}),
    }


def critic_speaker_identity_context(
    chunks: list[Any], relevant_speakers: set[str]
) -> list[dict[str, Any]]:
    if not relevant_speakers:
        return []
    output: list[dict[str, Any]] = []
    per_speaker_count: dict[str, int] = {}
    identity_re = re.compile(
        r"\b(?:i(?:'|\N{RIGHT SINGLE QUOTATION MARK})m|i am|this is|my name is)\b",
        re.IGNORECASE,
    )
    for chunk in chunks:
        for unit in evidence_units_for_text(str(chunk["text"] or "")):
            speaker = str(unit.get("speaker") or "")
            if speaker not in relevant_speakers:
                continue
            seen_count = per_speaker_count.get(speaker, 0)
            is_identity_unit = bool(identity_re.search(str(unit["text"])))
            if seen_count >= 2 and not is_identity_unit:
                continue
            output.append(
                {
                    "chunk_id": str(chunk["id"]),
                    "unit_id": str(unit["unit_id"]),
                    "speaker": speaker,
                    "text": str(unit["text"]),
                }
            )
            per_speaker_count[speaker] = seen_count + 1
            if len(output) >= 8:
                return output
    return output


def critic_known_participants(chunks: list[Any]) -> list[str]:
    output: list[str] = []
    in_participants = False
    for chunk in chunks:
        for raw_line in str(chunk["text"] or "").splitlines():
            line = raw_line.strip()
            if line.casefold() == "## known participants":
                in_participants = True
                continue
            if in_participants and line.startswith("## "):
                in_participants = False
            if in_participants and line.startswith("- "):
                participant = line[2:].strip()
                if participant and participant not in output:
                    output.append(participant)
            if len(output) >= 12:
                return output
    return output


def repair_fact_action_evidence(
    paths: BrainPaths,
    action: dict[str, Any],
    review: dict[str, Any],
) -> dict[str, Any]:
    if action.get("action_type") != "fact_upsert":
        return {"status": "not_repaired", "reason": "action is not fact_upsert"}
    requested_unit_ids = stable_critic_repair_unit_ids(
        review.get("repaired_evidence_unit_ids")
    )
    if not requested_unit_ids:
        return {
            "status": "not_repaired",
            "reason": "critic did not return repaired evidence unit ids",
        }
    context = critic_fact_source_context(paths, action)
    if not context or not context.get("available"):
        return {
            "status": "not_repaired",
            "reason": str(
                (context or {}).get("reason") or "source context unavailable"
            ),
        }
    original_unit_ids = critic_fact_evidence_unit_ids(
        (action_payload(action).get("fact") or {})
    )
    repaired_unit_ids = stable_unique_strings([*original_unit_ids, *requested_unit_ids])
    if len(repaired_unit_ids) > MAX_CRITIC_REPAIR_EVIDENCE_UNITS:
        return {
            "status": "not_repaired",
            "reason": (
                "critic evidence repair exceeds the maximum citation size; "
                "a bounded repair may not silently discard cited units"
            ),
            "requested_evidence_unit_ids": requested_unit_ids,
        }
    if repaired_unit_ids == original_unit_ids:
        return {
            "status": "not_repaired",
            "reason": "critic evidence repair did not add any citation units",
            "requested_evidence_unit_ids": requested_unit_ids,
        }
    repairable_ids = {
        str(unit.get("unit_id") or "")
        for unit in context.get("repairable_units") or []
        if isinstance(unit, dict)
    }
    invalid_ids = [
        unit_id for unit_id in repaired_unit_ids if unit_id not in repairable_ids
    ]
    if invalid_ids:
        return {
            "status": "not_repaired",
            "reason": "critic selected units outside the bounded repair context",
            "invalid_unit_ids": invalid_ids,
        }
    chunk_id = str(context["repairable_chunk_id"])
    rebuilt = rebuild_fact_action_evidence(
        paths,
        action,
        chunk_id=chunk_id,
        unit_ids=repaired_unit_ids,
    )
    if rebuilt["status"] != "repaired":
        return rebuilt
    return {
        **rebuilt,
        "original_evidence_unit_ids": original_unit_ids,
        "requested_evidence_unit_ids": requested_unit_ids,
    }


def rebuild_fact_action_evidence(
    paths: BrainPaths,
    action: dict[str, Any],
    *,
    chunk_id: str,
    unit_ids: list[str],
) -> dict[str, Any]:
    repaired_unit_ids = stable_critic_repair_unit_ids(unit_ids)
    if not repaired_unit_ids:
        return {"status": "not_repaired", "reason": "no evidence units provided"}
    with connection(paths.sqlite_path) as conn:
        row = conn.execute(
            "SELECT text FROM chunks WHERE id = ?", (chunk_id,)
        ).fetchone()
    if row is None:
        return {"status": "not_repaired", "reason": "repair chunk no longer exists"}
    resolved = resolve_evidence_unit_ids(
        str(row["text"] or ""),
        chunk_id=chunk_id,
        unit_ids=repaired_unit_ids,
    )
    if resolved["missing_unit_ids"] or not resolved["source_spans"]:
        return {
            "status": "not_repaired",
            "reason": "repaired unit ids did not resolve to exact source spans",
        }
    evidence = dict(action.get("evidence_json") or {})
    payload = dict(evidence.get("payload") or {})
    fact = dict(payload.get("fact") or {})
    metadata = dict(fact.get("metadata") or {})
    fact["source_ids"] = stable_unique_strings(
        [*(fact.get("source_ids") or []), *resolved["source_ids"]]
    )
    fact["source_spans"] = [
        span
        for span in fact.get("source_spans") or []
        if not isinstance(span, dict) or str(span.get("chunk_id") or "") != chunk_id
    ] + resolved["source_spans"]
    fact["evidence_quote"] = "\n...\n".join(resolved["quotes"])[:1000]
    fact["evidence_unit_ids"] = repaired_unit_ids
    metadata["evidence_units"] = resolved["evidence_units"]
    metadata["critic_evidence_repaired"] = True
    fact["metadata"] = metadata
    payload["fact"] = fact
    evidence["payload"] = payload
    action["evidence_json"] = evidence
    return {
        "status": "repaired",
        "chunk_id": chunk_id,
        "repaired_evidence_unit_ids": repaired_unit_ids,
        "repaired_source_spans": resolved["source_spans"],
    }


def critic_provider_label(provider: LLMProvider | None) -> str:
    if provider is None:
        return "critic:unknown"
    model = getattr(provider, "model", None)
    if model:
        return f"{provider.name}:{model}"
    return str(provider.name)


def evidence_with_critic_review(
    action: dict[str, Any], review: dict[str, Any]
) -> dict[str, Any]:
    evidence = dict(action.get("evidence_json") or {})
    evidence["critic_review"] = {
        "critic_by": review.get("critic_by"),
        "decision": review.get("decision"),
        "rationale": review.get("rationale") or "",
    }
    return evidence


def evidence_with_critic_repair(
    action: dict[str, Any],
    *,
    initial_review: dict[str, Any],
    repair: dict[str, Any],
    final_review: dict[str, Any],
) -> dict[str, Any]:
    evidence = dict(action.get("evidence_json") or {})
    evidence["critic_evidence_repair"] = {
        "initial_review": initial_review,
        "repair": repair,
        "final_review": final_review,
    }
    return evidence


def handle_critic_disagreement(
    paths: BrainPaths,
    action_id: str,
    decision: PolicyDecision,
    critic_disagreement_mode: str,
) -> None:
    if critic_disagreement_mode == "reject":
        reject_action(paths, action_id, decision, "critic did not agree")
        return
    mark_needs_human(paths, action_id, decision, "critic did not agree")


def apply_action(
    paths: BrainPaths,
    action_id: str,
    *,
    applied_status: str = "applied",
    allow_llm_entity_resolution: bool = True,
) -> dict[str, Any]:
    with connection(paths.sqlite_path) as conn:
        action = load_action(conn, action_id)
        if action["status"] in APPLIED_STATUSES:
            return action
        spec = ACTION_TYPE_SPECS.get(str(action["action_type"]))
        if spec is None:
            conn.execute(
                "UPDATE cos_actions SET status = ? WHERE id = ?",
                ("failed", action_id),
            )
            conn.commit()
            raise ValueError(f"unknown cos action type: {action['action_type']}")
        if not spec.get("implemented"):
            conn.execute(
                "UPDATE cos_actions SET status = ? WHERE id = ?",
                ("failed", action_id),
            )
            conn.commit()
            reason = spec.get("reason") or "not implemented"
            raise ValueError(
                f"action type {action['action_type']} is declared but not implemented: {reason}"
            )
        payload = action_payload(action)
        inverse: dict[str, Any] = {}
        target_fact_ids = list(action.get("target_fact_ids") or [])
        target_contract_ids = list(action.get("target_contract_ids") or [])
        target_page_paths = list(action.get("target_page_paths") or [])

        if action["action_type"] == "fact_upsert":
            fact_ids, inverse = apply_fact_upsert(
                conn,
                payload,
                paths=paths,
                allow_llm_entity_resolution=allow_llm_entity_resolution,
            )
            target_fact_ids = stable_unique([*target_fact_ids, *fact_ids])
        elif action["action_type"] in {"fact_supersede", "resolve_conflict"}:
            fact_ids, inverse = apply_fact_updates(conn, payload)
            target_fact_ids = stable_unique([*target_fact_ids, *fact_ids])
        elif action["action_type"] == "fact_merge":
            fact_ids, inverse = apply_fact_merge(conn, payload)
            target_fact_ids = stable_unique([*target_fact_ids, *fact_ids])
        elif action["action_type"] == "display_contested":
            fact_ids, inverse = apply_display_contested(conn, payload, action_id)
            target_fact_ids = stable_unique([*target_fact_ids, *fact_ids])
        elif action["action_type"] == "entity_merge":
            fact_ids, inverse = apply_entity_merge(conn, payload)
            target_fact_ids = stable_unique([*target_fact_ids, *fact_ids])
        elif action["action_type"] == "entity_split":
            fact_ids, inverse = apply_entity_split(conn, payload)
            target_fact_ids = stable_unique([*target_fact_ids, *fact_ids])
        elif action["action_type"] == "rehome_fact":
            fact_id, inverse = apply_rehome_fact(conn, payload)
            target_fact_ids = stable_unique([*target_fact_ids, fact_id])
        elif action["action_type"] == "edit_contract":
            contract_id, inverse = apply_edit_contract(conn, payload)
            target_contract_ids = stable_unique([*target_contract_ids, contract_id])
        elif action["action_type"] == "synthesize_page":
            page_hint, inverse = apply_synthesize_page(conn, payload)
            target_page_paths = stable_unique([*target_page_paths, page_hint])
        elif action["action_type"] == "page_merge":
            fact_ids, contract_ids, page_hints, inverse = apply_page_merge(
                conn, payload
            )
            target_fact_ids = stable_unique([*target_fact_ids, *fact_ids])
            target_contract_ids = stable_unique([*target_contract_ids, *contract_ids])
            target_page_paths = stable_unique([*target_page_paths, *page_hints])
        elif action["action_type"] == "page_split":
            fact_ids, page_hints, inverse = apply_page_split(conn, payload)
            target_fact_ids = stable_unique([*target_fact_ids, *fact_ids])
            target_page_paths = stable_unique([*target_page_paths, *page_hints])
        elif action["action_type"] == "rename_page":
            fact_ids, contract_ids, page_hints, inverse = apply_rename_page(
                conn, payload
            )
            target_fact_ids = stable_unique([*target_fact_ids, *fact_ids])
            target_contract_ids = stable_unique([*target_contract_ids, *contract_ids])
            target_page_paths = stable_unique([*target_page_paths, *page_hints])
        elif action["action_type"] in {
            "canonicalize_page",
            "archive_page",
            "revert_page_snapshot",
        }:
            page_hint = str(payload.get("page_hint") or "")
            if page_hint:
                target_page_paths = stable_unique([*target_page_paths, page_hint])
            inverse = {"noop": True}
        else:
            conn.execute(
                "UPDATE cos_actions SET status = ? WHERE id = ?",
                ("failed", action_id),
            )
            conn.commit()
            raise ValueError(
                f"unsupported implemented action type: {action['action_type']}"
            )

        state_hash = target_state_hash(
            conn,
            target_fact_ids=target_fact_ids,
            target_contract_ids=target_contract_ids,
            target_page_paths=target_page_paths,
        )
        conn.execute(
            """
            UPDATE cos_actions
            SET status = ?, target_fact_ids = ?, target_contract_ids = ?,
                target_page_paths = ?, inverse_action_json = ?,
                applied_state_hash = ?, applied_at = ?
            WHERE id = ?
            """,
            (
                applied_status,
                dumps(target_fact_ids),
                dumps(target_contract_ids),
                dumps(target_page_paths),
                dumps(inverse),
                state_hash,
                now_iso(),
                action_id,
            ),
        )
        retire_open_candidate_siblings(
            conn,
            action,
            reason=f"candidate resolved by {applied_status} action",
        )
    return get_action(paths, action_id)


def revert_action(paths: BrainPaths, action_id: str) -> dict[str, Any]:
    with connection(paths.sqlite_path) as conn:
        action = load_action(conn, action_id)
        if action["status"] == "reverted":
            return action
        expected = action.get("applied_state_hash")
        current = target_state_hash(
            conn,
            target_fact_ids=action.get("target_fact_ids") or [],
            target_contract_ids=action.get("target_contract_ids") or [],
            target_page_paths=action.get("target_page_paths") or [],
        )
        if expected and current != expected:
            create_action_residue(
                conn,
                action,
                "revert_drift",
                "Guarded revert refused because target state drifted after apply.",
            )
            conn.execute(
                "UPDATE cos_actions SET status = ? WHERE id = ?",
                ("failed", action_id),
            )
            return load_action(conn, action_id)
        apply_inverse(conn, action.get("inverse_action_json") or {})
        conn.execute(
            """
            UPDATE cos_actions
            SET status = 'reverted', reverted_at = ?
            WHERE id = ?
            """,
            (now_iso(), action_id),
        )
    return get_action(paths, action_id)


def audit_action_reviewability(conn: Any, action: dict[str, Any]) -> dict[str, Any]:
    if str(action.get("audit_status") or "") != "sampled_bad":
        return {
            "reviewable": False,
            "revertible": False,
            "revert_mode": None,
            "reason": "audit_not_flagged",
        }
    if str(action.get("status") or "") not in APPLIED_STATUSES:
        return {
            "reviewable": False,
            "revertible": False,
            "revert_mode": None,
            "reason": "action_no_longer_applied",
        }
    inverse = action.get("inverse_action_json")
    meaningful_inverse = isinstance(inverse, dict) and any(
        key != "noop" and bool(value) for key, value in inverse.items()
    )
    expected = str(action.get("applied_state_hash") or "").strip()
    if expected:
        current = target_state_hash(
            conn,
            target_fact_ids=action.get("target_fact_ids") or [],
            target_contract_ids=action.get("target_contract_ids") or [],
            target_page_paths=action.get("target_page_paths") or [],
        )
        if current != expected:
            fact_id = audited_active_fact_id(conn, action)
            if fact_id:
                return {
                    "reviewable": True,
                    "revertible": True,
                    "revert_mode": "reject_current_fact",
                    "fact_id": fact_id,
                    "reason": "audited_fact_still_active_after_related_drift",
                }
            return {
                "reviewable": False,
                "revertible": False,
                "revert_mode": None,
                "reason": "applied_state_drifted",
            }
    result = {
        "reviewable": True,
        "revertible": meaningful_inverse,
        "revert_mode": "action" if meaningful_inverse else None,
        "reason": "current_applied_state",
    }
    fact_id = audited_active_fact_id(conn, action)
    if fact_id:
        result["fact_id"] = fact_id
    return result


def audited_active_fact_id(conn: Any, action: dict[str, Any]) -> str | None:
    if str(action.get("action_type") or "") != "fact_upsert":
        return None
    payload = action_payload(action)
    fact = payload.get("fact") if isinstance(payload, dict) else None
    if not isinstance(fact, dict):
        return None
    statement = " ".join(str(fact.get("statement") or "").split())
    fact_ids = [
        str(fact_id)
        for fact_id in [fact.get("id"), *(action.get("target_fact_ids") or [])]
        if str(fact_id or "").strip()
    ]
    fact_ids = list(dict.fromkeys(fact_ids))
    if not fact_ids or not statement:
        return None
    placeholders = ",".join("?" for _ in fact_ids)
    for current in conn.execute(
        f"SELECT id, statement, status FROM facts WHERE id IN ({placeholders})",
        fact_ids,
    ):
        if (
            str(current["status"] or "") in {"active", "contested"}
            and " ".join(str(current["statement"] or "").split()) == statement
        ):
            return str(current["id"])
    return None


def reviewable_bad_audit_actions(conn: Any) -> list[dict[str, Any]]:
    actions = [
        row_to_action(row)
        for row in conn.execute(
            """
            SELECT *
            FROM cos_actions
            WHERE audit_status = 'sampled_bad'
            ORDER BY COALESCE(applied_at, created_at) DESC, id DESC
            """
        )
    ]
    return [
        action
        for action in actions
        if audit_action_reviewability(conn, action)["reviewable"]
    ]


def repair_refused_fact_audit_revert(
    paths: BrainPaths, action_id: str
) -> dict[str, Any]:
    timestamp = now_iso()
    with connection(paths.sqlite_path) as conn:
        action = load_action(conn, action_id)
        if (
            action.get("action_type") != "fact_upsert"
            or action.get("status") != "failed"
            or action.get("audit_status") != "sampled_bad"
        ):
            raise ValueError("action is not a failed sampled-bad fact revert")
        fact_id = audited_active_fact_id(conn, action)
        if not fact_id:
            raise ValueError("audited fact is no longer active")
        residue_rows = list(
            conn.execute(
                """
                SELECT id
                FROM open_questions
                WHERE action_id = ?
                  AND kind = 'revert_drift'
                  AND status IN ('open', 'needs_human')
                """,
                (action_id,),
            )
        )
        if not residue_rows:
            raise ValueError("action has no active refused-revert residue")
        evidence = dict(action.get("evidence_json") or {})
        evidence["audit_queue_reconciliation"] = {
            "version": "audit-queue-v1",
            "outcome": "restored_applied_status_after_refused_revert",
            "fact_id": fact_id,
            "residue_question_ids": [str(row["id"]) for row in residue_rows],
            "reconciled_at": timestamp,
        }
        conn.execute(
            "UPDATE cos_actions SET status = 'applied', evidence_json = ? WHERE id = ?",
            (dumps(evidence), action_id),
        )
        conn.execute(
            """
            UPDATE open_questions
            SET status = 'auto_resolved', answer = ?, answered_at = ?,
                decided_by = 'audit_queue_reconciliation_v1'
            WHERE action_id = ?
              AND kind = 'revert_drift'
              AND status IN ('open', 'needs_human')
            """,
            (
                dumps(
                    {
                        "decision": "restored_audit_review",
                        "action_id": action_id,
                        "fact_id": fact_id,
                    }
                ),
                timestamp,
                action_id,
            ),
        )
    return {
        "status": "repaired",
        "action": get_action(paths, action_id),
        "resolved_question_ids": [str(row["id"]) for row in residue_rows],
    }


def record_action_audit(
    paths: BrainPaths,
    action_id: str,
    audit_status: str,
    *,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    with connection(paths.sqlite_path) as conn:
        action = load_action(conn, action_id)
        evidence = dict(action.get("evidence_json") or {})
        audits = list(evidence.get("audits") or [])
        audits.append(
            {"status": audit_status, "metadata": metadata or {}, "at": now_iso()}
        )
        evidence["audits"] = audits
        conn.execute(
            """
            UPDATE cos_actions
            SET audit_status = ?, evidence_json = ?
            WHERE id = ?
            """,
            (audit_status, dumps(evidence), action_id),
        )
    return get_action(paths, action_id)


def mark_simple_autonomy_applied(
    paths: BrainPaths,
    action_id: str,
    *,
    autonomy_level: str = "L2",
    policy_decision: str = "simple_autonomy",
) -> dict[str, Any]:
    with connection(paths.sqlite_path) as conn:
        conn.execute(
            """
            UPDATE cos_actions
            SET policy_decision = ?, autonomy_level = ?
            WHERE id = ?
            """,
            (policy_decision, autonomy_level, action_id),
        )
    return get_action(paths, action_id)


def mark_action_residue(
    paths: BrainPaths,
    action_id: str,
    *,
    kind: str,
    reason: str,
    policy_decision: str = "simple_residue",
    autonomy_level: str = "L3",
) -> dict[str, Any]:
    with connection(paths.sqlite_path) as conn:
        action = load_action(conn, action_id)
        create_action_residue(conn, action, kind, reason)
        conn.execute(
            """
            UPDATE cos_actions
            SET status = 'needs_human', policy_decision = ?, autonomy_level = ?
            WHERE id = ?
            """,
            (policy_decision, autonomy_level, action_id),
        )
    return get_action(paths, action_id)


def reject_action(
    paths: BrainPaths, action_id: str, decision: PolicyDecision, reason: str
) -> dict[str, Any]:
    rejected_at = now_iso()
    with connection(paths.sqlite_path) as conn:
        action = load_action(conn, action_id)
        evidence = dict(action.get("evidence_json") or {})
        evidence["rejection"] = {
            "reason": reason,
            "rejected_at": rejected_at,
            "policy_id": decision.policy_id,
            "policy_version": decision.policy_version,
            "policy_decision": decision.policy_decision,
            "autonomy_level": decision.autonomy_level,
        }
        conn.execute(
            """
            UPDATE cos_actions
            SET status = 'rejected', policy_id = ?, policy_version = ?,
                policy_decision = ?, autonomy_level = ?, evidence_json = ?
            WHERE id = ?
            """,
            (
                decision.policy_id,
                decision.policy_version,
                decision.policy_decision,
                decision.autonomy_level,
                dumps(evidence),
                action_id,
            ),
        )
        retire_open_candidate_siblings(
            conn,
            action,
            reason="candidate rejected by policy",
        )
    return get_action(paths, action_id)


def apply_fact_upsert(
    conn: Any,
    payload: dict[str, Any],
    *,
    paths: BrainPaths | None = None,
    allow_llm_entity_resolution: bool = True,
    record_revision: bool = True,
) -> tuple[list[str], dict[str, Any]]:
    from .wiki_facts import rebuild_fact_retrieval_index

    raw_fact = payload.get("fact") if isinstance(payload.get("fact"), dict) else payload
    fact = dict(raw_fact)
    fact_id = str(fact.get("id") or new_id("fact"))
    existing = conn.execute("SELECT * FROM facts WHERE id = ?", (fact_id,)).fetchone()
    fact, entity_links = fact_with_entity_links(
        conn,
        fact,
        existing,
        paths=paths,
        allow_llm_entity_resolution=allow_llm_entity_resolution,
    )
    fact = event_time_for_resolved_primary_event(conn, fact, entity_links)
    touched_ids, inverse = write_versioned_fact(
        conn,
        fact,
        fact_id,
        existing,
        entity_links,
        record_revision=record_revision,
    )
    rebuild_fact_retrieval_index(conn)
    return touched_ids, inverse


def fact_with_entity_links(
    conn: Any,
    fact: dict[str, Any],
    existing: Any | None,
    *,
    paths: BrainPaths | None = None,
    allow_llm_entity_resolution: bool = True,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    existing_get = existing_value_getter(existing)
    mentions = fact_entity_mentions(fact)
    primary_mention = first_primary_entity_mention(mentions)
    admit_kinds = entity_admit_kinds(paths)
    entity_id = str(
        fact.get("entity_id") or existing_get("entity_id", "") or ""
    ).strip()
    links: list[dict[str, Any]] = []
    if entity_id and entity_exists(conn, entity_id):
        links.append(
            entity_link_from_existing(
                entity_id,
                primary_mention,
                fact,
                is_primary=True,
            )
        )
    else:
        mention_text = (
            str(primary_mention.get("surface") or "").strip()
            if primary_mention
            else primary_entity_mention(fact)
        )
        if not mention_text:
            return fact, []
        primary_resolution = resolve_entity(
            conn,
            mention_text,
            type_hint=primary_mention.get("entity_type")
            if primary_mention
            else fact.get("entity_type"),
            source_ids=source_ids_from_fact(fact),
            create=True,
            mention_kind=primary_mention.get("mention_kind")
            if primary_mention
            else "named",
            admit_kinds=admit_kinds,
            paths=paths if allow_llm_entity_resolution else None,
            context=entity_resolution_context(fact),
        )
        if primary_resolution is not None:
            entity_id = primary_resolution.entity_id
            links.append(
                entity_link_from_resolution(
                    primary_resolution,
                    primary_mention,
                    fact,
                    is_primary=True,
                )
            )
    for mention in mentions:
        if mention.get("is_primary"):
            continue
        resolution = resolve_entity(
            conn,
            str(mention.get("surface") or ""),
            type_hint=mention.get("entity_type"),
            source_ids=source_ids_from_fact(fact),
            create=True,
            mention_kind=mention.get("mention_kind"),
            admit_kinds=admit_kinds,
            paths=paths if allow_llm_entity_resolution else None,
            context=entity_resolution_context(fact),
        )
        if resolution is None:
            continue
        links.append(
            entity_link_from_resolution(resolution, mention, fact, is_primary=False)
        )
    if not entity_id and links:
        links[0]["is_primary"] = True
        entity_id = str(links[0]["entity_id"])
    if not entity_id:
        return fact, links
    return {**fact, "entity_id": entity_id}, links


def fact_entity_mentions(fact: dict[str, Any]) -> list[dict[str, Any]]:
    raw_mentions = fact.get("entity_mentions")
    if raw_mentions is None:
        metadata = fact.get("metadata")
        if isinstance(metadata, str):
            metadata = loads(metadata, {})
        if isinstance(metadata, dict):
            raw_mentions = metadata.get("model_entity_mentions")
    mentions = normalize_fact_entity_mentions(raw_mentions)
    if mentions:
        return mentions
    primary = primary_entity_mention(fact)
    if not primary:
        return []
    return [
        {
            "surface": primary,
            "entity_type": normalize_entity_type(fact.get("entity_type")),
            "is_primary": True,
            "mention_span": None,
            "mention_kind": "named",
            "confidence": optional_float(
                fact.get("truth_confidence") or fact.get("confidence")
            ),
        }
    ]


def normalize_fact_entity_mentions(raw_mentions: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_mentions, list):
        return []
    mentions: list[dict[str, Any]] = []
    primary_seen = False
    for raw in raw_mentions:
        if not isinstance(raw, dict):
            continue
        surface = str(
            raw.get("surface")
            or raw.get("mention")
            or raw.get("name")
            or raw.get("entity_key")
            or ""
        ).strip()
        if not surface:
            continue
        entity_type = normalize_entity_type(raw.get("entity_type") or raw.get("type"))
        mention_kind = normalize_mention_kind(
            raw.get("mention_kind") or raw.get("kind")
        )
        is_primary = bool(raw.get("is_primary")) and not primary_seen
        if is_primary:
            primary_seen = True
        mentions.append(
            {
                "surface": surface,
                "entity_type": entity_type,
                "is_primary": is_primary,
                "mention_span": raw.get("mention_span")
                if isinstance(raw.get("mention_span"), dict)
                else None,
                "mention_kind": mention_kind,
                "confidence": optional_float(raw.get("confidence")),
            }
        )
    if mentions and not any(mention["is_primary"] for mention in mentions):
        mentions[0]["is_primary"] = True
    return mentions


def first_primary_entity_mention(
    mentions: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if not mentions:
        return None
    return next(
        (mention for mention in mentions if mention.get("is_primary")), mentions[0]
    )


def entity_link_from_existing(
    entity_id: str,
    mention: dict[str, Any] | None,
    fact: dict[str, Any],
    *,
    is_primary: bool,
) -> dict[str, Any]:
    return {
        "entity_id": entity_id,
        "is_primary": is_primary,
        "mention_text": str(
            (mention or {}).get("surface") or primary_entity_mention(fact) or ""
        ),
        "mention_span": (mention or {}).get("mention_span"),
        "mention_kind": (mention or {}).get("mention_kind"),
        "resolution_method": "exact",
        "confidence": (mention or {}).get("confidence")
        or optional_float(fact.get("truth_confidence") or fact.get("confidence")),
    }


def entity_link_from_resolution(
    resolution: EntityResolution,
    mention: dict[str, Any] | None,
    fact: dict[str, Any],
    *,
    is_primary: bool,
) -> dict[str, Any]:
    return {
        "entity_id": resolution.entity_id,
        "is_primary": is_primary,
        "mention_text": resolution.mention_text,
        "mention_span": (mention or {}).get("mention_span"),
        "mention_kind": (mention or {}).get("mention_kind"),
        "resolution_method": resolution.resolution_method,
        "confidence": (mention or {}).get("confidence")
        or optional_float(fact.get("truth_confidence") or fact.get("confidence")),
    }


def entity_admit_kinds(paths: BrainPaths | None) -> set[str]:
    if paths is None:
        return set(DEFAULT_ADMIT_KINDS)
    raw_config = load_cos_llm_config(paths).get("entity")
    if not isinstance(raw_config, dict):
        return set(DEFAULT_ADMIT_KINDS)
    return normalize_admit_kinds(raw_config.get("admit_kinds"))


def entity_resolution_context(fact: dict[str, Any]) -> dict[str, Any]:
    return {
        "statement": fact.get("statement"),
        "page_hint": fact.get("page_hint"),
        "section_hint": fact.get("section_hint"),
        "evidence_quote": fact.get("evidence_quote"),
    }


def primary_entity_mention(fact: dict[str, Any]) -> str | None:
    metadata = fact.get("metadata")
    if isinstance(metadata, str):
        metadata = loads(metadata, {})
    if isinstance(metadata, dict):
        model_entity_key = str(metadata.get("model_entity_key") or "").strip()
        if model_entity_key:
            return model_entity_key
    for key in ("entity_mention", "entity_name"):
        value = str(fact.get(key) or "").strip()
        if value:
            return value
    return None


def source_ids_from_fact(fact: dict[str, Any]) -> list[str]:
    source_ids = fact.get("source_ids")
    if isinstance(source_ids, str):
        return [str(item) for item in loads(source_ids, []) if str(item or "").strip()]
    if isinstance(source_ids, list):
        return [str(item) for item in source_ids if str(item or "").strip()]
    return []


def entity_exists(conn: Any, entity_id: str) -> bool:
    return (
        conn.execute("SELECT 1 FROM entities WHERE id = ?", (entity_id,)).fetchone()
        is not None
    )


def optional_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def apply_rehome_fact(conn: Any, payload: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    from .wiki_facts import rebuild_fact_retrieval_index, row_to_fact

    fact_id = str(payload.get("fact_id") or "")
    if not fact_id:
        raise ValueError("rehome_fact requires fact_id")
    row = conn.execute(
        "SELECT * FROM facts WHERE id = ? AND knowledge_to IS NULL", (fact_id,)
    ).fetchone()
    if not row:
        raise ValueError(f"fact not found: {fact_id}")
    old = row_to_fact(row)
    metadata = payload.get("metadata")
    metadata_sql = dumps(metadata) if isinstance(metadata, dict) else row["metadata"]
    conn.execute(
        """
        UPDATE facts
        SET page_hint = COALESCE(?, page_hint),
            entity_key = COALESCE(?, entity_key),
            section_hint = COALESCE(?, section_hint),
            metadata = ?,
            last_seen_at = ?
        WHERE id = ?
        """,
        (
            payload.get("page_hint"),
            payload.get("entity_key"),
            payload.get("section_hint"),
            metadata_sql,
            now_iso(),
            fact_id,
        ),
    )
    rebuild_fact_retrieval_index(conn)
    return (
        fact_id,
        {
            "restore_fact_routing": [
                {
                    "fact_id": fact_id,
                    "old_page_hint": old.get("page_hint"),
                    "old_entity_key": old.get("entity_key"),
                    "old_section_hint": old.get("section_hint"),
                    "old_metadata": old.get("metadata"),
                }
            ]
        },
    )


def apply_fact_updates(
    conn: Any, payload: dict[str, Any]
) -> tuple[list[str], dict[str, Any]]:
    from .wiki_facts import rebuild_fact_retrieval_index, row_to_fact

    updates = payload.get("updates") if isinstance(payload.get("updates"), list) else []
    if not updates:
        raise ValueError("fact update action requires updates")
    valid_updates = [update for update in updates if isinstance(update, dict)]
    update_fact_ids = [str(update.get("fact_id") or "") for update in valid_updates]
    if not valid_updates or any(not fact_id for fact_id in update_fact_ids):
        raise ValueError("fact update requires fact_id")
    if len(set(update_fact_ids)) != len(update_fact_ids):
        raise ValueError("fact update action requires distinct fact_ids")
    fact_ids: list[str] = []
    inverse: dict[str, Any] = {}
    for update, fact_id in zip(valid_updates, update_fact_ids):
        row = conn.execute("SELECT * FROM facts WHERE id = ?", (fact_id,)).fetchone()
        if not row:
            raise ValueError(f"fact not found: {fact_id}")
        old = row_to_fact(row)
        merged = dict(old)
        for key, value in update.items():
            if key != "fact_id":
                merged[key] = value
        touched, fragment = apply_fact_upsert(conn, {"fact": merged})
        fact_ids.extend(touched)
        merge_fact_inverse(inverse, fragment)
    rebuild_fact_retrieval_index(conn)
    return stable_unique(fact_ids), inverse


def apply_fact_merge(
    conn: Any, payload: dict[str, Any]
) -> tuple[list[str], dict[str, Any]]:
    from .wiki_facts import rebuild_fact_retrieval_index, row_to_fact

    keeper = (
        payload.get("keeper_fact")
        if isinstance(payload.get("keeper_fact"), dict)
        else {}
    )
    keeper_id = str(keeper.get("id") or "")
    superseded_fact_ids = [
        str(item) for item in payload.get("superseded_fact_ids") or [] if item
    ]
    fact_ids = stable_unique([keeper_id, *superseded_fact_ids])
    if not keeper_id or not superseded_fact_ids:
        raise ValueError("fact_merge requires keeper_fact.id and superseded_fact_ids")
    if len(set(superseded_fact_ids)) != len(superseded_fact_ids):
        raise ValueError("fact_merge requires distinct superseded_fact_ids")
    if keeper_id in superseded_fact_ids:
        raise ValueError("fact_merge keeper cannot supersede itself")
    old_facts: list[dict[str, Any]] = []
    for fact_id in fact_ids:
        row = conn.execute("SELECT * FROM facts WHERE id = ?", (fact_id,)).fetchone()
        if not row:
            raise ValueError(f"fact not found: {fact_id}")
        old_facts.append(row_to_fact(row))
    stored_keeper = next(fact for fact in old_facts if fact["id"] == keeper_id)
    proposed_keeper = {**stored_keeper, **keeper}
    reject_incompatible_fact_merge(proposed_keeper, old_facts)
    inverse: dict[str, Any] = {}
    touched, fragment = apply_fact_upsert(
        conn, {"fact": {**keeper, "status": keeper.get("status") or "active"}}
    )
    fact_ids.extend(touched)
    merge_fact_inverse(inverse, fragment)
    for fact_id in superseded_fact_ids:
        old = next(fact for fact in old_facts if fact["id"] == fact_id)
        touched, fragment = apply_fact_upsert(
            conn,
            {
                "fact": {
                    **old,
                    "status": "superseded",
                    "conflict_group_id": None,
                }
            },
        )
        fact_ids.extend(touched)
        merge_fact_inverse(inverse, fragment)
    rebuild_fact_retrieval_index(conn)
    return stable_unique(fact_ids), inverse


def apply_display_contested(
    conn: Any, payload: dict[str, Any], action_id: str
) -> tuple[list[str], dict[str, Any]]:
    from .wiki_facts import (
        ensure_open_conflict_question,
        rebuild_fact_retrieval_index,
        row_to_fact,
    )

    fact_ids = [str(item) for item in payload.get("fact_ids") or [] if item]
    conflict_group_id = str(payload.get("conflict_group_id") or new_id("factconflict"))
    if len(fact_ids) < 2:
        raise ValueError("display_contested requires at least two fact_ids")
    if len(set(fact_ids)) != len(fact_ids):
        raise ValueError("display_contested requires distinct fact_ids")
    facts: list[dict[str, Any]] = []
    touched_fact_ids: list[str] = list(fact_ids)
    inverse: dict[str, Any] = {}
    for fact_id in fact_ids:
        row = conn.execute("SELECT * FROM facts WHERE id = ?", (fact_id,)).fetchone()
        if not row:
            raise ValueError(f"fact not found: {fact_id}")
        fact = row_to_fact(row)
        facts.append(
            {**fact, "status": "conflicted", "conflict_group_id": conflict_group_id}
        )
        touched, fragment = apply_fact_upsert(
            conn,
            {
                "fact": {
                    **fact,
                    "status": "conflicted",
                    "conflict_group_id": conflict_group_id,
                }
            },
        )
        touched_fact_ids.extend(touched)
        merge_fact_inverse(inverse, fragment)
    question_id = ensure_open_conflict_question(
        conn, conflict_group_id, facts, fact_ids, action_id=action_id
    )
    rebuild_fact_retrieval_index(conn)
    if question_id:
        inverse["delete_question_ids"] = [question_id]
    return stable_unique(touched_fact_ids), inverse


def apply_entity_merge(
    conn: Any, payload: dict[str, Any]
) -> tuple[list[str], dict[str, Any]]:
    from .wiki_facts import rebuild_fact_retrieval_index

    canonical_id = str(
        payload.get("canonical_entity_id")
        or payload.get("target_entity_id")
        or payload.get("destination_entity_id")
        or ""
    ).strip()
    source_ids = entity_merge_source_ids(payload, canonical_id)
    if not canonical_id or not source_ids:
        raise ValueError(
            "entity_merge requires canonical_entity_id and merged_entity_ids"
        )
    entity_ids = stable_unique([canonical_id, *source_ids])
    entity_rows = entity_rows_by_id(conn, entity_ids)
    missing = [entity_id for entity_id in entity_ids if entity_id not in entity_rows]
    if missing:
        raise ValueError(f"entity_merge entity not found: {', '.join(missing)}")
    canonical = entity_rows[canonical_id]
    require_active_entity(canonical, role="canonical")
    pending_source_ids: list[str] = []
    for source_id in source_ids:
        source = entity_rows[source_id]
        source_status = str(source["status"] or "active")
        if source_status == "active":
            pending_source_ids.append(source_id)
            continue
        if (
            source_status == "merged"
            and str(source["merged_into"] or "") == canonical_id
        ):
            continue
        require_active_entity(source, role="merged")
    if not pending_source_ids:
        return [], {
            "noop": True,
            "already_applied": {
                "canonical_entity_id": canonical_id,
                "merged_entity_ids": source_ids,
            },
        }
    source_ids = pending_source_ids
    guard_entity_merge_types(
        canonical, [entity_rows[source_id] for source_id in source_ids]
    )

    link_rows = fact_entity_rows_for_entity_ids(conn, source_ids)
    denorm_rows = fact_denorm_rows_for_entity_ids(conn, source_ids)
    affected_fact_ids = stable_unique(
        [
            *[str(row["fact_id"]) for row in link_rows],
            *[str(row["id"]) for row in denorm_rows],
        ]
    )
    inverse = {
        "restore_entities": [dict(entity_rows[entity_id]) for entity_id in entity_ids],
        "restore_fact_entity_links": [dict(row) for row in link_rows],
        "restore_fact_entity_denorms": [
            {"fact_id": str(row["id"]), "entity_id": row["entity_id"]}
            for row in denorm_rows
        ],
    }
    denorm_fact_ids = {str(row["id"]) for row in denorm_rows}
    touched_fact_ids: list[str] = []
    source_id_set = set(source_ids)
    for fact_id in affected_fact_ids:
        revised_links = [
            {
                **link,
                "entity_id": canonical_id
                if str(link.get("entity_id") or "") in source_id_set
                else link.get("entity_id"),
            }
            for link in stored_fact_entity_links(conn, fact_id)
        ]
        touched, fragment = revise_stored_fact(
            conn,
            fact_id,
            {"entity_id": canonical_id} if fact_id in denorm_fact_ids else {},
            entity_links=revised_links,
        )
        touched_fact_ids.extend(touched)
        merge_fact_inverse(inverse, fragment)
    placeholders = ",".join("?" for _ in source_ids)
    canonical_updates = merged_canonical_entity_updates(
        canonical,
        [entity_rows[source_id] for source_id in source_ids],
    )
    conn.execute(
        "UPDATE entities SET aliases = ?, source_ids = ?, entity_type = ? WHERE id = ?",
        (
            canonical_updates["aliases"],
            canonical_updates["source_ids"],
            canonical_updates["entity_type"],
            canonical_id,
        ),
    )
    conn.execute(
        f"""
        UPDATE entities
        SET status = 'merged', merged_into = ?
        WHERE id IN ({placeholders})
        """,
        [canonical_id, *source_ids],
    )
    rebuild_fact_retrieval_index(conn)
    return stable_unique(touched_fact_ids), inverse


def apply_entity_split(
    conn: Any, payload: dict[str, Any]
) -> tuple[list[str], dict[str, Any]]:
    from .wiki_facts import rebuild_fact_retrieval_index

    restore_payload = entity_split_restore_payload(payload)
    if not entity_restore_payload_has_content(restore_payload):
        raise ValueError("entity_split requires merge_inverse or restore_* payload")
    desired_entities = [
        row
        for row in restore_payload.get("restore_entities") or []
        if isinstance(row, dict)
    ]
    desired_facts = {
        str(row.get("id")): row
        for row in restore_payload.get("restore_facts") or []
        if isinstance(row, dict) and row.get("id")
    }
    desired_entity_ids = {
        str(row.get("fact_id")): row.get("entity_id")
        for row in restore_payload.get("restore_fact_entity_denorms") or []
        if isinstance(row, dict) and row.get("fact_id")
    }
    desired_links: dict[str, list[dict[str, Any]]] = {}
    for restore in restore_payload.get("restore_revision_head_links") or []:
        if isinstance(restore, dict) and restore.get("fact_id"):
            desired_links[str(restore["fact_id"])] = list(restore.get("links") or [])
    versioned_link_fact_ids = set(desired_links)
    for row in restore_payload.get("restore_fact_entity_links") or []:
        if not isinstance(row, dict) or not row.get("fact_id"):
            continue
        fact_id = str(row["fact_id"])
        if fact_id in versioned_link_fact_ids:
            continue
        mention_span = row.get("mention_span")
        desired_links.setdefault(fact_id, []).append(
            {
                **row,
                "mention_span": loads(mention_span, None)
                if isinstance(mention_span, str)
                else mention_span,
            }
        )
    fact_ids = stable_unique([*desired_facts, *desired_entity_ids, *desired_links])
    entity_ids = [str(row.get("id") or "") for row in desired_entities]
    inverse: dict[str, Any] = {
        "restore_entities": [
            dict(row) for row in entity_rows_by_id(conn, entity_ids).values()
        ]
    }
    touched_fact_ids: list[str] = []
    for fact_id in fact_ids:
        desired_fact = desired_facts.get(fact_id) or {}
        updates = (
            {"entity_id": desired_fact.get("entity_id")}
            if "entity_id" in desired_fact
            else (
                {"entity_id": desired_entity_ids[fact_id]}
                if fact_id in desired_entity_ids
                else {}
            )
        )
        touched, fragment = revise_stored_fact(
            conn,
            fact_id,
            updates,
            entity_links=desired_links.get(fact_id),
        )
        touched_fact_ids.extend(touched)
        merge_fact_inverse(inverse, fragment)
    for row in desired_entities:
        restore_entity_row(conn, row)
    rebuild_fact_retrieval_index(conn)
    return stable_unique(touched_fact_ids), inverse


def entity_merge_source_ids(payload: dict[str, Any], canonical_id: str) -> list[str]:
    raw = (
        payload.get("merged_entity_ids")
        or payload.get("source_entity_ids")
        or payload.get("entity_ids")
        or []
    )
    if not isinstance(raw, list):
        raw = [raw]
    return stable_unique(
        [
            str(item).strip()
            for item in raw
            if str(item or "").strip() and str(item).strip() != canonical_id
        ]
    )


def entity_rows_by_id(conn: Any, entity_ids: list[str]) -> dict[str, Any]:
    entity_ids = stable_unique(entity_ids)
    if not entity_ids:
        return {}
    placeholders = ",".join("?" for _ in entity_ids)
    return {
        str(row["id"]): row
        for row in conn.execute(
            f"SELECT * FROM entities WHERE id IN ({placeholders})",
            entity_ids,
        )
    }


def require_active_entity(row: Any, *, role: str) -> None:
    status = str(row["status"] or "active")
    if status != "active":
        raise ValueError(f"entity_merge {role} entity is not active: {row['id']}")


def guard_entity_merge_types(canonical: Any, source_rows: list[Any]) -> None:
    canonical_type = normalize_entity_type(canonical["entity_type"])
    for source in source_rows:
        source_type = normalize_entity_type(source["entity_type"])
        if canonical_type and source_type and canonical_type != source_type:
            raise ValueError(
                "entity_merge type mismatch: "
                f"{canonical['id']} is {canonical_type}, {source['id']} is {source_type}"
            )


def fact_entity_rows_for_entity_ids(conn: Any, entity_ids: list[str]) -> list[Any]:
    entity_ids = stable_unique(entity_ids)
    if not entity_ids:
        return []
    placeholders = ",".join("?" for _ in entity_ids)
    return list(
        conn.execute(
            f"""
            SELECT fe.*
            FROM fact_entities fe
            JOIN facts f ON f.id = fe.fact_id
            WHERE fe.entity_id IN ({placeholders})
              AND f.knowledge_to IS NULL
            ORDER BY fe.fact_id, fe.is_primary DESC, fe.id
            """,
            entity_ids,
        )
    )


def fact_denorm_rows_for_entity_ids(conn: Any, entity_ids: list[str]) -> list[Any]:
    entity_ids = stable_unique(entity_ids)
    if not entity_ids:
        return []
    placeholders = ",".join("?" for _ in entity_ids)
    return list(
        conn.execute(
            f"""
            SELECT id, entity_id
            FROM facts
            WHERE entity_id IN ({placeholders})
              AND knowledge_to IS NULL
            ORDER BY id
            """,
            entity_ids,
        )
    )


def merged_canonical_entity_updates(
    canonical: Any, source_rows: list[Any]
) -> dict[str, Any]:
    aliases = [
        str(item) for item in loads(canonical["aliases"], []) if str(item or "").strip()
    ]
    alias_norms = {normalize_entity_name(alias) for alias in aliases}
    canonical_norm = normalize_entity_name(canonical["name"])
    for source in source_rows:
        for candidate in [source["name"], *loads(source["aliases"], [])]:
            text = str(candidate or "").strip()
            normalized = normalize_entity_name(text)
            if (
                not text
                or not normalized
                or normalized in {canonical_norm, *alias_norms}
            ):
                continue
            aliases.append(text)
            alias_norms.add(normalized)
    source_ids = stable_unique(
        [
            *[
                str(item)
                for item in loads(canonical["source_ids"], [])
                if str(item or "").strip()
            ],
            *[
                str(item)
                for source in source_rows
                for item in loads(source["source_ids"], [])
                if str(item or "").strip()
            ],
        ]
    )
    entity_type = normalize_entity_type(canonical["entity_type"]) or next(
        (
            normalize_entity_type(source["entity_type"])
            for source in source_rows
            if normalize_entity_type(source["entity_type"])
        ),
        None,
    )
    return {
        "aliases": dumps(aliases),
        "source_ids": dumps(source_ids),
        "entity_type": entity_type,
    }


def entity_split_restore_payload(payload: dict[str, Any]) -> dict[str, Any]:
    for key in ("merge_inverse", "inverse_action_json", "inverse"):
        candidate = payload.get(key)
        if isinstance(candidate, dict):
            return candidate
    return payload


def entity_restore_payload_has_content(payload: dict[str, Any]) -> bool:
    return any(
        payload.get(key)
        for key in (
            "restore_entities",
            "restore_fact_entity_links",
            "restore_fact_entity_denorms",
        )
    )


def restore_entity_merge_inverse(conn: Any, inverse: dict[str, Any]) -> list[str]:
    from .wiki_facts import rebuild_fact_retrieval_index

    entity_rows = [
        row for row in inverse.get("restore_entities") or [] if isinstance(row, dict)
    ]
    link_rows = [
        row
        for row in inverse.get("restore_fact_entity_links") or []
        if isinstance(row, dict)
    ]
    denorm_rows = [
        row
        for row in inverse.get("restore_fact_entity_denorms") or []
        if isinstance(row, dict)
    ]
    for row in entity_rows:
        restore_entity_row(conn, row)
    affected_fact_ids = stable_unique(
        [
            *[str(row.get("fact_id") or "") for row in denorm_rows],
            *[str(row.get("fact_id") or "") for row in link_rows],
        ]
    )
    if not inverse.get("restore_facts"):
        for fact_id in affected_fact_ids:
            links = [
                {
                    **row,
                    "mention_span": loads(row.get("mention_span"), None)
                    if isinstance(row.get("mention_span"), str)
                    else row.get("mention_span"),
                }
                for row in link_rows
                if str(row.get("fact_id")) == fact_id
            ]
            if links:
                replace_fact_entity_links(conn, fact_id=fact_id, links=links)
        for row in denorm_rows:
            conn.execute(
                "UPDATE facts SET entity_id = ? WHERE id = ? AND knowledge_to IS NULL",
                (row.get("entity_id"), row.get("fact_id")),
            )
    rebuild_fact_retrieval_index(conn)
    return affected_fact_ids


def restore_entity_row(conn: Any, row: dict[str, Any]) -> None:
    entity_id = str(row.get("id") or "")
    if not entity_id:
        return
    existing = conn.execute(
        "SELECT 1 FROM entities WHERE id = ?", (entity_id,)
    ).fetchone()
    values = (
        row.get("name") or "Unknown Entity",
        normalize_entity_type(row.get("entity_type")),
        row.get("aliases") if row.get("aliases") is not None else "[]",
        row.get("status") or "active",
        row.get("merged_into"),
        row.get("description"),
        row.get("source_ids") if row.get("source_ids") is not None else "[]",
        row.get("created_at") or now_iso(),
    )
    if existing:
        conn.execute(
            """
            UPDATE entities
            SET name = ?, entity_type = ?, aliases = ?, status = ?,
                merged_into = ?, description = ?, source_ids = ?, created_at = ?
            WHERE id = ?
            """,
            (*values, entity_id),
        )
        return
    conn.execute(
        """
        INSERT INTO entities(
          id, name, entity_type, aliases, status, merged_into, description, source_ids, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (entity_id, *values),
    )


def apply_edit_contract(
    conn: Any, payload: dict[str, Any]
) -> tuple[str, dict[str, Any]]:
    contract = (
        payload.get("contract")
        if isinstance(payload.get("contract"), dict)
        else payload
    )
    contract_id = str(contract.get("id") or new_id("contract"))
    existing = conn.execute(
        "SELECT * FROM page_contracts WHERE id = ?", (contract_id,)
    ).fetchone()
    inverse = (
        {"restore_contract": dict(existing)}
        if existing
        else {"delete_contract_ids": [contract_id]}
    )
    timestamp = now_iso()
    values = (
        contract_id,
        str(contract.get("page_hint") or (existing["page_hint"] if existing else "")),
        contract.get(
            "canonical_entity", existing["canonical_entity"] if existing else None
        ),
        contract.get("page_scope", existing["page_scope"] if existing else None),
        contract.get(
            "retrieval_purpose", existing["retrieval_purpose"] if existing else None
        ),
        contract.get(
            "what_belongs_here", existing["what_belongs_here"] if existing else None
        ),
        contract.get(
            "what_does_not_belong_here",
            existing["what_does_not_belong_here"] if existing else None,
        ),
        contract.get(
            "freshness_policy", existing["freshness_policy"] if existing else None
        ),
        dumps(
            contract.get(
                "related_pages",
                loads(existing["related_pages"], []) if existing else [],
            )
        ),
        int(contract.get("version", existing["version"] if existing else 1)),
        str(contract.get("status", existing["status"] if existing else "active")),
        existing["created_at"] if existing else timestamp,
        timestamp,
    )
    if not values[1]:
        raise ValueError("contract page_hint is required")
    conn.execute(
        """
        INSERT OR REPLACE INTO page_contracts(
          id, page_hint, canonical_entity, page_scope, retrieval_purpose,
          what_belongs_here, what_does_not_belong_here, freshness_policy,
          related_pages, version, status, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        values,
    )
    return contract_id, inverse


def apply_synthesize_page(
    conn: Any, payload: dict[str, Any]
) -> tuple[str, dict[str, Any]]:
    synthesis = (
        payload.get("synthesis")
        if isinstance(payload.get("synthesis"), dict)
        else payload
    )
    synthesis_id = str(synthesis.get("id") or new_id("synthesis"))
    page_hint = str(synthesis.get("page_hint") or "")
    if not page_hint:
        raise ValueError("synthesize_page requires page_hint")
    conn.execute(
        "UPDATE wiki_page_syntheses SET stale = 1 WHERE page_hint = ? AND stale = 0",
        (page_hint,),
    )
    conn.execute(
        """
        INSERT INTO wiki_page_syntheses(
          id, page_hint, synthesis_markdown, fact_ids, fact_hash,
          model, prompt_version, generated_at, stale
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            synthesis_id,
            page_hint,
            str(synthesis.get("synthesis_markdown") or ""),
            dumps(synthesis.get("fact_ids") or []),
            synthesis.get("fact_hash"),
            synthesis.get("model"),
            synthesis.get("prompt_version"),
            now_iso(),
            int(bool(synthesis.get("stale", False))),
        ),
    )
    return page_hint, {"delete_synthesis_ids": [synthesis_id]}


def apply_page_merge(
    conn: Any, payload: dict[str, Any]
) -> tuple[list[str], list[str], list[str], dict[str, Any]]:
    from .wiki_facts import rebuild_fact_retrieval_index

    topology = topology_payload(payload)
    page_hints = stable_unique(
        [str(item) for item in topology.get("page_hints") or [] if item]
    )
    destination = str(topology.get("destination_page_hint") or "")
    if not destination:
        destination = choose_merge_destination(conn, page_hints)
    source_pages = [page_hint for page_hint in page_hints if page_hint != destination]
    if not destination or not source_pages:
        raise ValueError(
            "page_merge requires at least one source page and one destination page"
        )

    old_facts = facts_for_page_hints(conn, source_pages)
    old_contracts = contracts_for_page_hints(conn, page_hints)
    old_syntheses = syntheses_for_page_hints(conn, page_hints)
    timestamp = now_iso()
    fact_ids = [fact["id"] for fact in old_facts]
    for fact in old_facts:
        conn.execute(
            """
            UPDATE facts
            SET page_hint = ?, metadata = ?, last_seen_at = ?
            WHERE id = ?
            """,
            (
                destination,
                dumps(
                    topology_metadata(
                        fact.get("metadata"),
                        "page_merge",
                        old_page_hint=fact.get("page_hint"),
                    )
                ),
                timestamp,
                fact["id"],
            ),
        )
    for contract in old_contracts:
        if contract["page_hint"] in source_pages:
            conn.execute(
                "UPDATE page_contracts SET status = ?, updated_at = ? WHERE id = ?",
                ("superseded", timestamp, contract["id"]),
            )
    mark_syntheses_stale(conn, page_hints)
    rebuild_fact_retrieval_index(conn)
    return (
        fact_ids,
        [str(contract["id"]) for contract in old_contracts],
        stable_unique([*source_pages, destination]),
        {
            "restore_facts": old_facts,
            "restore_contracts": old_contracts,
            "restore_syntheses": old_syntheses,
        },
    )


def apply_page_split(
    conn: Any, payload: dict[str, Any]
) -> tuple[list[str], list[str], dict[str, Any]]:
    from .wiki_facts import rebuild_fact_retrieval_index

    topology = topology_payload(payload)
    page_hint = str(
        topology.get("page_hint")
        or topology.get("source_page_hint")
        or first_value(topology.get("page_hints"))
        or ""
    )
    if not page_hint:
        raise ValueError("page_split requires page_hint")
    facts = facts_for_page_hints(conn, [page_hint], statuses=["active"])
    movable = [
        fact
        for fact in facts
        if normalized_section(str(fact.get("section_hint") or ""))
        not in {"", "summary", "unsectioned"}
    ]
    if not movable:
        raise ValueError("page_split requires active non-summary facts to move")
    old_syntheses = syntheses_for_page_hints(conn, [page_hint])
    moved_pages: list[str] = []
    timestamp = now_iso()
    for fact in movable:
        destination = split_destination_page_hint(
            page_hint, str(fact.get("section_hint") or "section")
        )
        moved_pages.append(destination)
        conn.execute(
            """
            UPDATE facts
            SET page_hint = ?, metadata = ?, last_seen_at = ?
            WHERE id = ?
            """,
            (
                destination,
                dumps(
                    topology_metadata(
                        fact.get("metadata"), "page_split", old_page_hint=page_hint
                    )
                ),
                timestamp,
                fact["id"],
            ),
        )
    affected_pages = stable_unique([page_hint, *moved_pages])
    old_syntheses.extend(syntheses_for_page_hints(conn, moved_pages))
    mark_syntheses_stale(conn, affected_pages)
    rebuild_fact_retrieval_index(conn)
    return (
        [str(fact["id"]) for fact in movable],
        affected_pages,
        {
            "restore_facts": movable,
            "restore_syntheses": old_syntheses,
        },
    )


def apply_rename_page(
    conn: Any, payload: dict[str, Any]
) -> tuple[list[str], list[str], list[str], dict[str, Any]]:
    from .wiki_facts import rebuild_fact_retrieval_index

    topology = topology_payload(payload)
    source = str(
        topology.get("from_page_hint")
        or topology.get("source_page_hint")
        or topology.get("old_page_hint")
        or ""
    )
    destination = str(
        topology.get("to_page_hint")
        or topology.get("destination_page_hint")
        or topology.get("new_page_hint")
        or ""
    )
    if not source or not destination:
        raise ValueError("rename_page requires source and destination page hints")
    old_facts = facts_for_page_hints(conn, [source])
    old_contracts = contracts_for_page_hints(conn, [source])
    old_syntheses = syntheses_for_page_hints(conn, [source, destination])
    timestamp = now_iso()
    conn.execute(
        """
        UPDATE facts
        SET page_hint = ?, metadata = ?, last_seen_at = ?
        WHERE page_hint = ? AND knowledge_to IS NULL
        """,
        (
            destination,
            dumps(topology_metadata({}, "rename_page", old_page_hint=source)),
            timestamp,
            source,
        ),
    )
    for fact in old_facts:
        merged_metadata = topology_metadata(
            fact.get("metadata"), "rename_page", old_page_hint=source
        )
        conn.execute(
            "UPDATE facts SET metadata = ? WHERE id = ?",
            (dumps(merged_metadata), fact["id"]),
        )
    conn.execute(
        "UPDATE page_contracts SET page_hint = ?, updated_at = ? WHERE page_hint = ?",
        (destination, timestamp, source),
    )
    conn.execute(
        "UPDATE wiki_page_syntheses SET page_hint = ?, stale = 1 WHERE page_hint = ?",
        (destination, source),
    )
    conn.execute(
        "UPDATE wiki_page_syntheses SET stale = 1 WHERE page_hint = ?", (destination,)
    )
    conn.execute("UPDATE wiki_pages SET path = ? WHERE path = ?", (destination, source))
    rebuild_fact_retrieval_index(conn)
    return (
        [str(fact["id"]) for fact in old_facts],
        [str(contract["id"]) for contract in old_contracts],
        [source, destination],
        {
            "restore_facts": old_facts,
            "restore_contracts": old_contracts,
            "restore_syntheses": old_syntheses,
            "restore_wiki_page_paths": [{"old_path": source, "new_path": destination}],
        },
    )


def topology_payload(payload: dict[str, Any]) -> dict[str, Any]:
    candidate = (
        payload.get("candidate") if isinstance(payload.get("candidate"), dict) else None
    )
    return candidate or payload


def first_value(value: Any) -> Any:
    if isinstance(value, list) and value:
        return value[0]
    return None


def choose_merge_destination(conn: Any, page_hints: list[str]) -> str:
    if len(page_hints) < 2:
        return page_hints[0] if page_hints else ""
    counts = fact_counts_for_pages(conn, page_hints)
    contract_pages = set(contract_page_hints(conn, page_hints))
    return sorted(
        page_hints,
        key=lambda page_hint: (
            page_hint not in contract_pages,
            -counts.get(page_hint, 0),
            len(page_hint),
            page_hint,
        ),
    )[0]


def fact_counts_for_pages(conn: Any, page_hints: list[str]) -> dict[str, int]:
    if not page_hints:
        return {}
    placeholders = ",".join("?" for _ in page_hints)
    return {
        str(row["page_hint"]): int(row["count"])
        for row in conn.execute(
            f"""
            SELECT page_hint, COUNT(*) AS count
            FROM facts
            WHERE page_hint IN ({placeholders})
              AND status = 'active'
            GROUP BY page_hint
            """,
            page_hints,
        )
    }


def contract_page_hints(conn: Any, page_hints: list[str]) -> list[str]:
    if not page_hints:
        return []
    placeholders = ",".join("?" for _ in page_hints)
    return [
        str(row["page_hint"])
        for row in conn.execute(
            f"""
            SELECT DISTINCT page_hint
            FROM page_contracts
            WHERE page_hint IN ({placeholders})
              AND status = 'active'
            """,
            page_hints,
        )
    ]


def facts_for_page_hints(
    conn: Any,
    page_hints: list[str],
    *,
    statuses: list[str] | None = None,
) -> list[dict[str, Any]]:
    from .wiki_facts import row_to_fact

    page_hints = stable_unique(page_hints)
    if not page_hints:
        return []
    page_placeholders = ",".join("?" for _ in page_hints)
    params: list[Any] = list(page_hints)
    status_clause = ""
    if statuses:
        status_placeholders = ",".join("?" for _ in statuses)
        status_clause = f" AND status IN ({status_placeholders})"
        params.extend(statuses)
    return [
        row_to_fact(row)
        for row in conn.execute(
            f"""
            SELECT *
            FROM facts
            WHERE page_hint IN ({page_placeholders})
              AND knowledge_to IS NULL
            {status_clause}
            ORDER BY page_hint, id
            """,
            params,
        )
    ]


def contracts_for_page_hints(conn: Any, page_hints: list[str]) -> list[dict[str, Any]]:
    page_hints = stable_unique(page_hints)
    if not page_hints:
        return []
    placeholders = ",".join("?" for _ in page_hints)
    return [
        dict(row)
        for row in conn.execute(
            f"""
            SELECT *
            FROM page_contracts
            WHERE page_hint IN ({placeholders})
            ORDER BY page_hint, id
            """,
            page_hints,
        )
    ]


def syntheses_for_page_hints(conn: Any, page_hints: list[str]) -> list[dict[str, Any]]:
    page_hints = stable_unique(page_hints)
    if not page_hints:
        return []
    placeholders = ",".join("?" for _ in page_hints)
    return [
        dict(row)
        for row in conn.execute(
            f"""
            SELECT *
            FROM wiki_page_syntheses
            WHERE page_hint IN ({placeholders})
            ORDER BY page_hint, generated_at, id
            """,
            page_hints,
        )
    ]


def mark_syntheses_stale(conn: Any, page_hints: list[str]) -> None:
    page_hints = stable_unique(page_hints)
    if not page_hints:
        return
    placeholders = ",".join("?" for _ in page_hints)
    conn.execute(
        f"UPDATE wiki_page_syntheses SET stale = 1 WHERE page_hint IN ({placeholders})",
        page_hints,
    )


def topology_metadata(
    metadata: Any, operation: str, *, old_page_hint: Any
) -> dict[str, Any]:
    base = loads(metadata, {}) if isinstance(metadata, str) else metadata
    if not isinstance(base, dict):
        base = {}
    history = list(base.get("topology_history") or [])
    history.append(
        {"operation": operation, "old_page_hint": old_page_hint, "at": now_iso()}
    )
    return {**base, "topology_history": history}


def normalized_section(section: str) -> str:
    return section.strip().lower()


def split_destination_page_hint(page_hint: str, section: str) -> str:
    path = Path(page_hint)
    suffix = path.suffix or ".md"
    stem = path.stem or "page"
    section_slug = slugify_text(section)
    return (path.with_name(f"{stem}-{section_slug}{suffix}")).as_posix()


def slugify_text(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug[:80] or "section"


def restore_contract(conn: Any, contract: dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO page_contracts(
          id, page_hint, canonical_entity, page_scope, retrieval_purpose,
          what_belongs_here, what_does_not_belong_here, freshness_policy,
          related_pages, version, status, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            contract["id"],
            contract["page_hint"],
            contract.get("canonical_entity"),
            contract.get("page_scope"),
            contract.get("retrieval_purpose"),
            contract.get("what_belongs_here"),
            contract.get("what_does_not_belong_here"),
            contract.get("freshness_policy"),
            contract.get("related_pages", "[]"),
            contract.get("version", 1),
            contract.get("status", "active"),
            contract.get("created_at") or now_iso(),
            contract.get("updated_at"),
        ),
    )


def restore_synthesis(conn: Any, synthesis: dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO wiki_page_syntheses(
          id, page_hint, synthesis_markdown, fact_ids, fact_hash,
          model, prompt_version, generated_at, stale
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            synthesis["id"],
            synthesis["page_hint"],
            synthesis.get("synthesis_markdown", ""),
            synthesis.get("fact_ids", "[]"),
            synthesis.get("fact_hash"),
            synthesis.get("model"),
            synthesis.get("prompt_version"),
            synthesis.get("generated_at") or now_iso(),
            int(synthesis.get("stale") or 0),
        ),
    )


def apply_inverse(conn: Any, inverse: dict[str, Any]) -> None:
    from .wiki_facts import rebuild_fact_retrieval_index

    for fact_id in inverse.get("delete_fact_ids") or []:
        conn.execute("DELETE FROM facts WHERE id = ?", (fact_id,))
    for fact in inverse.get("restore_facts") or []:
        apply_fact_upsert(conn, {"fact": fact}, record_revision=False)
    if inverse.get("restore_fact"):
        apply_fact_upsert(
            conn, {"fact": inverse["restore_fact"]}, record_revision=False
        )
    for restore in inverse.get("restore_revision_head_links") or []:
        replace_fact_entity_links(
            conn,
            fact_id=str(restore.get("fact_id") or ""),
            links=restore.get("links") or [],
        )
    for routing in inverse.get("restore_fact_routing") or []:
        conn.execute(
            """
            UPDATE facts
            SET page_hint = ?, entity_key = ?, section_hint = ?,
                metadata = COALESCE(?, metadata), last_seen_at = ?
            WHERE id = ? AND knowledge_to IS NULL
            """,
            (
                routing.get("old_page_hint"),
                routing.get("old_entity_key"),
                routing.get("old_section_hint"),
                dumps(routing["old_metadata"]) if "old_metadata" in routing else None,
                now_iso(),
                routing.get("fact_id"),
            ),
        )
    for contract_id in inverse.get("delete_contract_ids") or []:
        conn.execute("DELETE FROM page_contracts WHERE id = ?", (contract_id,))
    if inverse.get("restore_contract"):
        contract = dict(inverse["restore_contract"])
        restore_contract(conn, contract)
    for contract in inverse.get("restore_contracts") or []:
        restore_contract(conn, dict(contract))
    for synthesis in inverse.get("restore_syntheses") or []:
        restore_synthesis(conn, dict(synthesis))
    if entity_restore_payload_has_content(inverse):
        restore_entity_merge_inverse(conn, inverse)
    for path_restore in inverse.get("restore_wiki_page_paths") or []:
        conn.execute(
            "UPDATE wiki_pages SET path = ? WHERE path = ?",
            (path_restore.get("old_path"), path_restore.get("new_path")),
        )
    for synthesis_id in inverse.get("delete_synthesis_ids") or []:
        conn.execute("DELETE FROM wiki_page_syntheses WHERE id = ?", (synthesis_id,))
    for question_id in inverse.get("delete_question_ids") or []:
        conn.execute("DELETE FROM open_questions WHERE id = ?", (question_id,))
    rebuild_fact_retrieval_index(conn)


def mark_needs_human(
    paths: BrainPaths, action_id: str, decision: PolicyDecision, reason: str
) -> None:
    with connection(paths.sqlite_path) as conn:
        action = load_action(conn, action_id)
        create_action_residue(conn, action, "policy_escalation", reason)
        conn.execute(
            """
            UPDATE cos_actions
            SET status = 'needs_human', policy_id = ?, policy_version = ?,
                policy_decision = ?, autonomy_level = ?
            WHERE id = ?
            """,
            (
                decision.policy_id,
                decision.policy_version,
                decision.policy_decision,
                decision.autonomy_level,
                action_id,
            ),
        )


def create_action_residue(
    conn: Any, action: dict[str, Any], kind: str, question: str
) -> str:
    question_id = new_id("question")
    fields = action_residue_question_fields(conn, action, kind, question)
    conn.execute(
        """
        INSERT INTO open_questions(
          id, kind, entity_key, page_hint, fact_ids, question, options, status,
          context, action_id, recommended_action, risk_tier, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            question_id,
            kind,
            fields["entity_key"],
            fields["page_hint"],
            dumps(fields["fact_ids"]),
            fields["question"],
            dumps(fields["options"]),
            "needs_human",
            dumps(fields["context"]),
            action["id"],
            dumps(fields["recommended_action"]),
            action.get("risk_tier"),
            now_iso(),
        ),
    )
    return question_id


def refresh_action_residue_question(
    conn: Any,
    action: dict[str, Any],
    question_id: str,
    *,
    kind: str,
    reason: str,
) -> None:
    fields = action_residue_question_fields(conn, action, kind, reason)
    conn.execute(
        """
        UPDATE open_questions
        SET entity_key = ?, page_hint = ?, fact_ids = ?, question = ?,
            options = ?, context = ?, recommended_action = ?, risk_tier = ?,
            action_id = ?
        WHERE id = ?
        """,
        (
            fields["entity_key"],
            fields["page_hint"],
            dumps(fields["fact_ids"]),
            fields["question"],
            dumps(fields["options"]),
            dumps(fields["context"]),
            dumps(fields["recommended_action"]),
            action.get("risk_tier"),
            action["id"],
            question_id,
        ),
    )


def action_residue_question_fields(
    conn: Any, action: dict[str, Any], kind: str, question: str
) -> dict[str, Any]:
    base = {
        "entity_key": None,
        "page_hint": (action.get("target_page_paths") or [None])[0],
        "fact_ids": action.get("target_fact_ids") or [],
        "question": question,
        "options": [],
        "context": {"action_id": action["id"], "action_type": action["action_type"]},
        "recommended_action": {
            "action_type": action["action_type"],
            "payload": action_payload(action),
        },
    }
    if kind != "fact_conflict_review":
        return base
    conflict_fields = fact_conflict_review_question_fields(conn, action, question)
    return conflict_fields or base


def fact_conflict_review_question_fields(
    conn: Any, action: dict[str, Any], question: str
) -> dict[str, Any] | None:
    from .wiki_facts import compact_statement, question_options_for_facts, row_to_fact

    payload = action_payload(action)
    candidate = payload.get("fact") if isinstance(payload.get("fact"), dict) else {}
    counterpart_fact_ids = stable_unique(
        [
            *[str(item) for item in action.get("target_fact_ids") or [] if item],
            *[
                str(item)
                for item in (
                    (action.get("evidence_json") or {})
                    .get("resolver_precheck", {})
                    .get("counterpart_fact_ids", [])
                )
                if item
            ],
        ]
    )
    if not candidate or not counterpart_fact_ids:
        return None
    placeholders = ",".join("?" for _ in counterpart_fact_ids)
    counterpart_facts = [
        row_to_fact(row)
        for row in conn.execute(
            f"SELECT * FROM facts WHERE id IN ({placeholders}) ORDER BY observed_at DESC, created_at DESC",
            counterpart_fact_ids,
        )
    ]
    if not counterpart_facts:
        return None
    candidate_option = {
        "option_type": "candidate_fact",
        "action_id": action["id"],
        "label": f"Candidate: {compact_statement(candidate.get('statement'), 140)}",
        "statement": candidate.get("statement"),
        "confidence": candidate.get("confidence")
        or candidate.get("truth_confidence")
        or action.get("confidence"),
        "observed_at": candidate.get("observed_at"),
        "source_ids": candidate.get("source_ids") or [],
        "source_spans": candidate.get("source_spans") or [],
        "evidence_quote": candidate.get("evidence_quote"),
        "page_hint": candidate.get("page_hint"),
    }
    existing_options = [
        {**option, "option_type": "existing_fact"}
        for option in question_options_for_facts(counterpart_facts)
    ]
    first_fact = counterpart_facts[0]
    resolver_precheck = (action.get("evidence_json") or {}).get("resolver_precheck", {})
    return {
        "entity_key": candidate.get("entity_key") or first_fact.get("entity_key"),
        "page_hint": candidate.get("page_hint")
        or (action.get("target_page_paths") or [None])[0]
        or first_fact.get("page_hint"),
        "fact_ids": counterpart_fact_ids,
        "question": (
            f"{question} Review the candidate fact against the existing fact(s) "
            "before applying or rewriting it."
        ),
        "options": [candidate_option, *existing_options],
        "context": {
            "action_id": action["id"],
            "action_type": action["action_type"],
            "candidate_action_id": action["id"],
            "counterpart_fact_ids": counterpart_fact_ids,
            "resolver_precheck": resolver_precheck,
        },
        "recommended_action": {
            "action_type": action["action_type"],
            "payload": payload,
            "review_required": "conflict_precheck",
            "counterpart_fact_ids": counterpart_fact_ids,
        },
    }


def target_state_hash(
    conn: Any,
    *,
    target_fact_ids: list[str],
    target_contract_ids: list[str],
    target_page_paths: list[str],
) -> str:
    state: dict[str, Any] = {
        "facts": [],
        "fact_entities": [],
        "entities": [],
        "contracts": [],
        "syntheses": [],
    }
    if target_fact_ids:
        placeholders = ",".join("?" for _ in target_fact_ids)
        state["facts"] = [
            dict(row)
            for row in conn.execute(
                f"SELECT * FROM facts WHERE id IN ({placeholders}) ORDER BY id",
                target_fact_ids,
            )
        ]
        state["fact_entities"] = [
            dict(row)
            for row in conn.execute(
                f"""
                SELECT *
                FROM fact_entities
                WHERE fact_id IN ({placeholders})
                ORDER BY fact_id, is_primary DESC, id
                """,
                target_fact_ids,
            )
        ]
        entity_ids = stable_unique(
            [
                str(fact.get("entity_id") or "")
                for fact in state["facts"]
                if fact.get("entity_id")
            ]
            + [
                str(link.get("entity_id") or "")
                for link in state["fact_entities"]
                if link.get("entity_id")
            ]
        )
        if entity_ids:
            entity_placeholders = ",".join("?" for _ in entity_ids)
            state["entities"] = [
                dict(row)
                for row in conn.execute(
                    f"SELECT * FROM entities WHERE id IN ({entity_placeholders}) ORDER BY id",
                    entity_ids,
                )
            ]
    if target_contract_ids:
        placeholders = ",".join("?" for _ in target_contract_ids)
        state["contracts"] = [
            dict(row)
            for row in conn.execute(
                f"SELECT * FROM page_contracts WHERE id IN ({placeholders}) ORDER BY id",
                target_contract_ids,
            )
        ]
    if target_page_paths:
        placeholders = ",".join("?" for _ in target_page_paths)
        state["syntheses"] = [
            dict(row)
            for row in conn.execute(
                f"""
                SELECT *
                FROM wiki_page_syntheses
                WHERE page_hint IN ({placeholders})
                ORDER BY page_hint, generated_at, id
                """,
                target_page_paths,
            )
        ]
    return hashlib.sha256(dumps(state).encode("utf-8")).hexdigest()


def get_action(paths: BrainPaths, action_id: str) -> dict[str, Any]:
    with connection(paths.sqlite_path) as conn:
        return load_action(conn, action_id)


def recent_actions(paths: BrainPaths, *, limit: int = 50) -> list[dict[str, Any]]:
    with connection(paths.sqlite_path) as conn:
        return [
            row_to_action(row)
            for row in conn.execute(
                """
                SELECT *
                FROM cos_actions
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (limit,),
            )
        ]


def load_action(conn: Any, action_id: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM cos_actions WHERE id = ?", (action_id,)
    ).fetchone()
    if not row:
        raise ValueError(f"cos action not found: {action_id}")
    return row_to_action(row)


def row_to_action(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "run_id": row["run_id"],
        "action_type": row["action_type"],
        "status": row["status"],
        "target_fact_ids": loads(row["target_fact_ids"], []),
        "target_page_paths": loads(row["target_page_paths"], []),
        "target_contract_ids": loads(row["target_contract_ids"], []),
        "action_features": loads(row["action_features"], {}),
        "proposed_by": row["proposed_by"],
        "critic_by": row["critic_by"],
        "critic_decision": row["critic_decision"],
        "confidence": row["confidence"],
        "risk_tier": row["risk_tier"],
        "policy_id": row["policy_id"],
        "policy_version": row["policy_version"],
        "policy_decision": row["policy_decision"],
        "autonomy_level": row["autonomy_level"],
        "inverse_action_json": loads(row["inverse_action_json"], {}),
        "evidence_json": loads(row["evidence_json"], {}),
        "applied_state_hash": row["applied_state_hash"],
        "audit_status": row["audit_status"],
        "created_at": row["created_at"],
        "applied_at": row["applied_at"],
        "reverted_at": row["reverted_at"],
    }


def action_payload(action: dict[str, Any]) -> dict[str, Any]:
    evidence = action.get("evidence_json") or {}
    payload = evidence.get("payload") or {}
    return payload if isinstance(payload, dict) else {}


def validate_action_type(action_type: str) -> None:
    if action_type not in ACTION_TYPE_SPECS:
        raise ValueError(f"unknown cos action type: {action_type}")


def existing_value_getter(row: Any | None) -> Any:
    def get(key: str, default: Any = None) -> Any:
        if row is None:
            return default
        try:
            value = row[key]
        except (KeyError, IndexError, TypeError):
            return default
        return default if value is None else value

    return get


def stable_unique(values: list[str]) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not value or value in seen:
            continue
        seen.add(value)
        output.append(value)
    return output
