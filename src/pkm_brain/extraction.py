from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from .cos_actions import (
    apply_action,
    decide_action,
    mark_action_residue,
    mark_simple_autonomy_applied,
    propose_action,
)
from .db import connection, dumps, rows
from .llm import (
    LLMProvider,
    complete_json,
    cos_role_provider_configured,
    get_cos_role_provider,
    load_cos_llm_config,
)
from .paths import BrainPaths
from .util import new_id, now_iso
from .wiki_facts import (
    canonical_page_hint_for_fact,
    entity_key_for_change,
    facts_directly_conflict,
    row_to_fact,
    topic_for_path,
)


EXTRACTION_SCHEMA = {
    "type": "object",
    "required": ["facts"],
    "properties": {
        "facts": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["statement", "chunk_id", "evidence_quote", "claim_class"],
            },
        }
    },
}
EXTRACTION_PROMPT_VERSION = "extractor-quote-windows-v2"
EXTRACTION_STAGE = "extractor"
EXTRACTION_VALIDATION_ATTEMPTS = 2
DEFAULT_EXTRACTION_WINDOW_CHUNKS = 6
DEFAULT_EXTRACTION_WINDOW_OVERLAP_CHUNKS = 1
DEFAULT_ROUTING_HINT_LIMIT = 80
DEFAULT_SKIPPED_SOURCE_TYPES = {"agent_session_log"}
DEFAULT_FALLBACK_PAGE_HINTS = {"concepts/extracted-facts.md"}
TERMINAL_EXTRACTION_WATERMARK_STATUSES = {"ok", "extracted_empty"}
DURABLE_CLAIM_CLASSES = {
    "decision",
    "commitment",
    "preference",
    "role_or_responsibility",
    "project_state",
    "factual_update",
    "open_question",
}
NON_CLAIM_CLASSES = {
    "event_metadata",
    "transcript_mechanic",
    "pleasantry",
    "boilerplate",
    "non_claim",
}
CLAIM_CLASSES = DURABLE_CLAIM_CLASSES | NON_CLAIM_CLASSES
LOW_INFORMATION_LINE_PATTERNS = (
    re.compile(r"^---+$"),
    re.compile(r"^\.\.\.+$"),
    re.compile(r"^title\s*:\s*.+$", re.IGNORECASE),
    re.compile(r"^(event_)?(started|ended)_at\s*:\s*.+$", re.IGNORECASE),
    re.compile(r"^(created|updated|captured|ingested)_at\s*:\s*.+$", re.IGNORECASE),
    re.compile(r"^source_path\s*:\s*.+$", re.IGNORECASE),
    re.compile(r"^session_id\s*:\s*.+$", re.IGNORECASE),
    re.compile(r"^no\s+(summary|memo|transcript)\s+was\s+captured\.?$", re.IGNORECASE),
    re.compile(r"^\d{4}-\d{2}-\d{2}t\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:z|[+-]\d{2}:\d{2})?$", re.IGNORECASE),
)


def extract_recent_documents(
    paths: BrainPaths,
    *,
    limit: int = 10,
    shadow: bool = True,
    llm_provider: LLMProvider | None = None,
    provider: str | None = None,
    changed_only: bool = True,
    run_id: str | None = None,
) -> dict[str, Any]:
    if not cos_role_provider_configured(paths, "extractor", llm_provider=llm_provider, provider=provider):
        return {
            "status": "skipped",
            "reason": "No CoS LLM provider configured for extractor role",
            "shadow": shadow,
            "documents": [],
            "candidates": [],
            "actions": [],
        }
    active_provider = get_cos_role_provider(paths, "extractor", provider=provider, llm_provider=llm_provider)
    extractor_model = getattr(active_provider, "model", None)
    extraction_config = load_extraction_config(paths)
    documents = recent_source_cards(
        paths,
        limit=limit,
        changed_only=changed_only,
        extractor_model=extractor_model,
        prompt_version=EXTRACTION_PROMPT_VERSION,
        extraction_config=extraction_config,
    )
    if not documents:
        return {"status": "ok", "shadow": shadow, "documents": [], "candidates": [], "actions": []}
    routing_hints = load_extraction_routing_hints(
        paths,
        limit=int(extraction_config.get("routing_hints_limit") or DEFAULT_ROUTING_HINT_LIMIT),
    )
    candidates: list[dict[str, Any]] = []
    document_validations: list[dict[str, Any]] = []
    for document in documents:
        document_candidates: list[dict[str, Any]] = []
        window_validations: list[dict[str, Any]] = []
        for window in document.get("windows") or []:
            extraction_result = extract_facts_with_validation_retry(
                paths,
                source_window_card(document, window, routing_hints),
                provider=provider,
                llm_provider=active_provider,
                extractor_model=extractor_model,
            )
            for candidate in extraction_result["candidates"]:
                candidate.setdefault("metadata", {})["document_id"] = document["document_id"]
                candidate["metadata"]["window_id"] = window["window_id"]
            document_candidates.extend(extraction_result["candidates"])
            window_validations.append(
                {
                    "window_id": window["window_id"],
                    "chunk_ids": [chunk["chunk_id"] for chunk in window.get("chunks") or []],
                    **extraction_result["validation"],
                }
            )
        document_validation = aggregate_document_validation(document, window_validations, document_candidates)
        record_extraction_watermarks(
            paths,
            [document],
            extractor_model=extractor_model,
            run_id=run_id,
            candidate_count=len(document_candidates),
            status=extraction_watermark_status(document_validation, document_candidates),
            validation=document_validation,
        )
        candidates.extend(document_candidates)
        document_validations.append(document_validation)
    validation = aggregate_run_validation(document_validations, candidates)
    actions: list[dict[str, Any]] = []
    if not shadow:
        simple_autonomy = simple_autonomy_config(extraction_config)
        if simple_autonomy["enabled"]:
            actions = apply_simple_autonomy_candidates(
                paths,
                candidates,
                run_id=run_id,
                simple_autonomy=simple_autonomy,
            )
        else:
            actions = propose_policy_gated_candidates(paths, candidates, run_id=run_id)
    return {
        "status": "ok",
        "shadow": shadow,
        "documents": documents,
        "candidates": candidates,
        "actions": actions,
        "validation": validation,
        "document_validations": document_validations,
    }


def extract_facts_with_validation_retry(
    paths: BrainPaths,
    source_window: dict[str, Any],
    *,
    provider: str | None,
    llm_provider: LLMProvider,
    extractor_model: str | None,
    max_validation_attempts: int = EXTRACTION_VALIDATION_ATTEMPTS,
) -> dict[str, Any]:
    prompt = extraction_prompt(source_window)
    accepted: list[dict[str, Any]] = []
    accepted_keys: set[tuple[Any, ...]] = set()
    best_report: dict[str, Any] | None = None
    attempts: list[dict[str, Any]] = []
    last_report = empty_extraction_validation_report()
    total_proposed_count = 0
    total_rejected_count = 0
    total_dropped_count = 0
    accepted_from_retry_count = 0
    for attempt_index in range(max_validation_attempts):
        parsed = complete_json(
            prompt,
            schema=EXTRACTION_SCHEMA,
            provider=provider,
            role="extractor",
            llm_provider=llm_provider,
            paths=paths,
        )
        report = validate_extraction_payload(paths, parsed, extractor_model=extractor_model)
        total_proposed_count += int(report.get("raw_fact_count") or 0)
        total_rejected_count += int(report.get("rejected_count") or 0)
        total_dropped_count += int(report.get("dropped_count") or 0)
        accepted_before_attempt = len(accepted)
        for candidate in report["candidates"]:
            key = candidate_dedupe_key(candidate)
            if key in accepted_keys:
                continue
            accepted.append(candidate)
            accepted_keys.add(key)
        if attempt_index > 0:
            accepted_from_retry_count += len(accepted) - accepted_before_attempt
        last_report = report
        attempts.append(compact_validation_report(report, attempt=attempt_index + 1))
        if best_report is None or better_extraction_report(report, best_report):
            best_report = report
        if not extraction_needs_validation_retry(report):
            break
        if attempt_index == max_validation_attempts - 1:
            break
        prompt = extraction_validation_retry_prompt(source_window, parsed, report)
    if best_report is None:
        best_report = empty_extraction_validation_report()
    validation = compact_validation_report(last_report)
    validation["raw_fact_count"] = total_proposed_count
    validation["attempted_fact_count"] = total_proposed_count
    validation["accepted_count"] = len(accepted)
    validation["accepted_from_retry_count"] = accepted_from_retry_count
    validation["final_rejected_count"] = int(last_report.get("rejected_count") or 0)
    validation["total_rejected_count"] = total_rejected_count
    validation["dropped_count"] = total_dropped_count
    validation["final_dropped_count"] = int(last_report.get("dropped_count") or 0)
    validation["attempts"] = attempts
    validation["selected_attempt"] = selected_validation_attempt(attempts, compact_validation_report(best_report))
    return {
        "candidates": accepted,
        "validation": validation,
    }


def recent_source_cards(
    paths: BrainPaths,
    *,
    limit: int,
    changed_only: bool = False,
    extractor_model: str | None = None,
    prompt_version: str = EXTRACTION_PROMPT_VERSION,
    extraction_config: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    config = extraction_config or load_extraction_config(paths)
    window_max_chunks = int(config.get("window_max_chunks") or DEFAULT_EXTRACTION_WINDOW_CHUNKS)
    window_overlap_chunks = int(config.get("window_overlap_chunks") or DEFAULT_EXTRACTION_WINDOW_OVERLAP_CHUNKS)
    with connection(paths.sqlite_path) as conn:
        doc_rows = rows(
            conn,
            """
            SELECT *
            FROM documents
            WHERE status = 'active'
            ORDER BY ingested_at DESC, id DESC
            """,
        )
        output: list[dict[str, Any]] = []
        for document in doc_rows:
            policy = extraction_policy_for_source_type(config, str(document["source_type"]))
            if not policy["extract"]:
                continue
            chunk_rows = rows(
                conn,
                """
                SELECT *
                FROM chunks
                WHERE document_id = ?
                ORDER BY chunk_index
                """,
                (document["id"],),
            )
            chunks = [
                {
                    "chunk_id": row["id"],
                    "chunk_index": row["chunk_index"],
                    "heading_path": row["heading_path"],
                    "token_count": row["token_count"],
                    "text": row["text"],
                }
                for row in chunk_rows
            ]
            normalized_content = normalized_extraction_content(chunks)
            normalized_content_hash = normalized_extraction_content_hash(chunks)
            if changed_only and extraction_terminal_watermark_exists(
                conn,
                document_id=str(document["id"]),
                content_hash=normalized_content_hash,
                extractor_model=extractor_model,
                prompt_version=prompt_version,
            ):
                continue
            windows = (
                []
                if not normalized_content
                else build_document_windows(
                    str(document["id"]),
                    chunks,
                    max_chunks=window_max_chunks,
                    overlap_chunks=window_overlap_chunks,
                )
            )
            output.append(
                {
                    "document_id": document["id"],
                    "title": document["title"],
                    "source_type": document["source_type"],
                    "source_id": f"document:{document['id']}",
                    "content_hash": normalized_content_hash,
                    "raw_content_hash": document["content_hash"],
                    "normalized_content_empty": not bool(normalized_content),
                    "policy": policy,
                    "chunks": chunks,
                    "windows": windows,
                }
            )
            if len(output) >= limit:
                break
        return output


def extraction_terminal_watermark_exists(
    conn: Any,
    *,
    document_id: str,
    content_hash: str,
    extractor_model: str | None,
    prompt_version: str,
) -> bool:
    row = conn.execute(
        """
        SELECT 1
        FROM cos_stage_watermarks
        WHERE stage = ?
          AND document_id = ?
          AND content_hash = ?
          AND COALESCE(model, '') = COALESCE(?, '')
          AND prompt_version = ?
          AND status IN (?, ?)
        LIMIT 1
        """,
        (
            EXTRACTION_STAGE,
            document_id,
            content_hash,
            extractor_model,
            prompt_version,
            *sorted(TERMINAL_EXTRACTION_WATERMARK_STATUSES),
        ),
    ).fetchone()
    return row is not None


def load_extraction_config(paths: BrainPaths) -> dict[str, Any]:
    raw_config = load_cos_llm_config(paths).get("extraction")
    raw = raw_config if isinstance(raw_config, dict) else {}
    window = raw.get("window") if isinstance(raw.get("window"), dict) else {}
    source_types = raw.get("source_types") if isinstance(raw.get("source_types"), dict) else {}
    simple_autonomy = normalize_simple_autonomy_config(raw.get("simple_autonomy"))
    max_chunks = max(1, int(window.get("max_chunks") or DEFAULT_EXTRACTION_WINDOW_CHUNKS))
    overlap_chunks = max(0, int(window.get("overlap_chunks") or DEFAULT_EXTRACTION_WINDOW_OVERLAP_CHUNKS))
    if overlap_chunks >= max_chunks:
        overlap_chunks = max(0, max_chunks - 1)
    return {
        "source_types": source_types,
        "window_max_chunks": max_chunks,
        "window_overlap_chunks": overlap_chunks,
        "routing_hints_limit": max(0, int(raw.get("routing_hints_limit") or DEFAULT_ROUTING_HINT_LIMIT)),
        "simple_autonomy": simple_autonomy,
    }


def extraction_policy_for_source_type(config: dict[str, Any], source_type: str) -> dict[str, Any]:
    source_types = config.get("source_types") if isinstance(config.get("source_types"), dict) else {}
    policy = {
        "extract": source_type not in DEFAULT_SKIPPED_SOURCE_TYPES,
        "full_coverage": True,
    }
    default_policy = source_types.get("default")
    if isinstance(default_policy, dict):
        policy.update(normalize_extraction_policy(default_policy))
    source_policy = source_types.get(source_type)
    if isinstance(source_policy, dict):
        policy.update(normalize_extraction_policy(source_policy))
    return policy


def normalize_extraction_policy(value: dict[str, Any]) -> dict[str, bool]:
    output: dict[str, bool] = {}
    if "extract" in value:
        output["extract"] = bool(value["extract"])
    if "full_coverage" in value:
        output["full_coverage"] = bool(value["full_coverage"])
    return output


def normalize_simple_autonomy_config(value: Any) -> dict[str, Any]:
    raw = value if isinstance(value, dict) else {}
    fallback_page_hints = raw.get("fallback_page_hints")
    if not isinstance(fallback_page_hints, list):
        fallback_page_hints = sorted(DEFAULT_FALLBACK_PAGE_HINTS)
    return {
        "enabled": bool(raw.get("enabled", False)),
        "fallback_page_hints": {
            canonical_page_hint_for_fact(str(item))
            for item in fallback_page_hints
            if str(item or "").strip()
        }
        or set(DEFAULT_FALLBACK_PAGE_HINTS),
        "min_extraction_confidence": optional_float(raw.get("min_extraction_confidence")),
        "min_routing_confidence": optional_float(raw.get("min_routing_confidence")),
        "min_truth_confidence": optional_float(raw.get("min_truth_confidence")),
    }


def simple_autonomy_config(config: dict[str, Any]) -> dict[str, Any]:
    raw = config.get("simple_autonomy")
    if isinstance(raw, dict):
        return raw
    return normalize_simple_autonomy_config({})


def propose_policy_gated_candidates(
    paths: BrainPaths,
    candidates: list[dict[str, Any]],
    *,
    run_id: str | None,
) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    for candidate in candidates:
        decision = earned_fact_decision(paths, candidate)
        action_fact = decision.get("fact") if isinstance(decision.get("fact"), dict) else candidate
        action = propose_action(
            paths,
            "fact_upsert",
            run_id=run_id,
            action_payload={"fact": action_fact},
            action_features=earned_fact_action_features(candidate, decision),
            target_fact_ids=decision.get("target_fact_ids") or [],
            target_page_paths=[candidate["page_hint"]],
            proposed_by="extractor",
            confidence=candidate.get("truth_confidence"),
            risk_tier=None,
            decide=False,
        )
        if decision["decision"] == "residue":
            actions.append(
                mark_action_residue(
                    paths,
                    action["id"],
                    kind=str(decision["residue_kind"]),
                    reason=str(decision["reason"]),
                    policy_decision="earned_residue",
                )
            )
            continue
        actions.append(decide_action(paths, action["id"]))
    return actions


def earned_fact_decision(paths: BrainPaths, candidate: dict[str, Any]) -> dict[str, Any]:
    page_hint = canonical_page_hint_for_fact(str(candidate.get("page_hint") or ""))
    if page_hint in DEFAULT_FALLBACK_PAGE_HINTS:
        return simple_residue_decision(
            "unrouted_fact",
            "Extractor routed the candidate to the fallback page; review routing before applying.",
        )
    if not candidate.get("source_spans") or not candidate.get("evidence_quote"):
        return simple_residue_decision(
            "weak_evidence_fact",
            "Candidate does not have quote-derived source spans.",
        )
    duplicate = find_exact_duplicate_fact(paths, candidate)
    if duplicate is not None:
        return {
            "decision": "apply",
            "reason": "Exact duplicate fact; update existing fact with additional source evidence.",
            "risk_tier": "low",
            "fact_upsert_resolution": "exact_duplicate_source_union",
            "target_fact_ids": [duplicate["id"]],
            "fact": merge_candidate_into_existing_fact(duplicate, candidate),
        }
    conflict_reason = resolver_precheck_conflict_reason(paths, candidate)
    if conflict_reason:
        return simple_residue_decision("fact_conflict_review", conflict_reason)
    return {
        "decision": "apply",
        "reason": "Quote-backed, routed candidate with no resolver precheck conflict signal.",
        "risk_tier": "medium",
        "fact_upsert_resolution": "new_clean_fact",
        "target_fact_ids": [],
    }


def earned_fact_action_features(candidate: dict[str, Any], decision: dict[str, Any]) -> dict[str, Any]:
    page_hint = canonical_page_hint_for_fact(str(candidate.get("page_hint") or ""))
    quote_backed = bool(candidate.get("source_spans") and candidate.get("evidence_quote"))
    clean = (
        decision.get("decision") == "apply"
        and quote_backed
        and page_hint not in DEFAULT_FALLBACK_PAGE_HINTS
        and decision.get("fact_upsert_resolution") in {"new_clean_fact", "exact_duplicate_source_union"}
    )
    return {
        "candidate_signal": "source_extraction",
        "clean_fact_upsert": clean,
        "fact_upsert_resolution": decision.get("fact_upsert_resolution"),
        "simple_decision": decision.get("decision"),
        "residue_kind": decision.get("residue_kind"),
        "affected_fact_count": 1,
        "reversible": True,
        "truth_mutation": False,
        "quote_backed": quote_backed,
        "fallback_route": page_hint in DEFAULT_FALLBACK_PAGE_HINTS,
        "resolver_precheck": "passed" if clean else "residue",
        "confidence": candidate.get("truth_confidence"),
        "extraction_confidence": candidate.get("extraction_confidence"),
        "routing_confidence": candidate.get("routing_confidence"),
        "truth_confidence": candidate.get("truth_confidence"),
        "eval_gate": {"suite": "extraction", "requires_labels": True},
    }


def resolver_precheck_conflict_reason(paths: BrainPaths, candidate: dict[str, Any]) -> str | None:
    page_hint = candidate.get("page_hint")
    section_hint = candidate.get("section_hint")
    statement = str(candidate.get("statement") or "")
    with connection(paths.sqlite_path) as conn:
        fact_rows = rows(
            conn,
            """
            SELECT *
            FROM facts
            WHERE COALESCE(page_hint, '') = COALESCE(?, '')
              AND COALESCE(section_hint, '') = COALESCE(?, '')
              AND status IN ('active', 'conflicted')
            ORDER BY observed_at DESC, created_at DESC
            LIMIT 50
            """,
            (page_hint, section_hint),
        )
    for row in fact_rows:
        fact = row_to_fact(row)
        if fact.get("status") == "conflicted":
            return "Nearby facts are already contested; review this candidate with the existing conflict."
        if facts_directly_conflict(fact, {"statement": statement}):
            return "Candidate appears to contradict an existing nearby fact."
    return None


def apply_simple_autonomy_candidates(
    paths: BrainPaths,
    candidates: list[dict[str, Any]],
    *,
    run_id: str | None,
    simple_autonomy: dict[str, Any],
) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    for candidate in candidates:
        decision = simple_fact_decision(paths, candidate, simple_autonomy)
        action_fact = decision.get("fact") if isinstance(decision.get("fact"), dict) else candidate
        action = propose_action(
            paths,
            "fact_upsert",
            run_id=run_id,
            action_payload={"fact": action_fact},
            action_features=simple_fact_action_features(candidate, decision),
            target_fact_ids=decision.get("target_fact_ids") or [],
            target_page_paths=[candidate["page_hint"]],
            proposed_by="extractor",
            confidence=candidate.get("truth_confidence"),
            risk_tier=str(decision.get("risk_tier") or "medium"),
            decide=False,
        )
        if decision["decision"] == "apply":
            applied = apply_action(paths, action["id"], applied_status="auto_applied")
            actions.append(mark_simple_autonomy_applied(paths, applied["id"]))
            continue
        actions.append(
            mark_action_residue(
                paths,
                action["id"],
                kind=str(decision["residue_kind"]),
                reason=str(decision["reason"]),
            )
        )
    return actions


def simple_fact_decision(
    paths: BrainPaths,
    candidate: dict[str, Any],
    simple_autonomy: dict[str, Any],
) -> dict[str, Any]:
    page_hint = canonical_page_hint_for_fact(str(candidate.get("page_hint") or ""))
    if page_hint in set(simple_autonomy.get("fallback_page_hints") or DEFAULT_FALLBACK_PAGE_HINTS):
        return simple_residue_decision(
            "unrouted_fact",
            "Extractor routed the candidate to the fallback page; review routing before applying.",
        )
    if not candidate.get("source_spans") or not candidate.get("evidence_quote"):
        return simple_residue_decision(
            "weak_evidence_fact",
            "Candidate does not have quote-derived source spans.",
        )
    low_confidence_reason = simple_low_confidence_reason(candidate, simple_autonomy)
    if low_confidence_reason:
        return simple_residue_decision("low_confidence_fact", low_confidence_reason)
    duplicate = find_exact_duplicate_fact(paths, candidate)
    if duplicate is not None:
        return {
            "decision": "apply",
            "reason": "Exact duplicate fact; update existing fact with additional source evidence.",
            "risk_tier": "low",
            "simple_resolution": "exact_duplicate_source_union",
            "target_fact_ids": [duplicate["id"]],
            "fact": merge_candidate_into_existing_fact(duplicate, candidate),
        }
    return {
        "decision": "apply",
        "reason": "Quote-backed, routed candidate with no deterministic residue signal.",
        "risk_tier": "medium",
        "simple_resolution": "new_fact",
        "target_fact_ids": [],
    }


def simple_residue_decision(kind: str, reason: str) -> dict[str, Any]:
    return {
        "decision": "residue",
        "residue_kind": kind,
        "reason": reason,
        "risk_tier": "high",
        "target_fact_ids": [],
    }


def simple_low_confidence_reason(candidate: dict[str, Any], simple_autonomy: dict[str, Any]) -> str | None:
    checks = (
        ("extraction_confidence", "Extraction confidence"),
        ("routing_confidence", "Routing confidence"),
        ("truth_confidence", "Truth confidence"),
    )
    for key, label in checks:
        threshold = simple_autonomy.get(f"min_{key}")
        if threshold is None:
            continue
        value = optional_float(candidate.get(key))
        if value is None or value < float(threshold):
            return f"{label} is below simple-autonomy threshold."
    return None


def simple_fact_action_features(candidate: dict[str, Any], decision: dict[str, Any]) -> dict[str, Any]:
    return {
        "candidate_signal": "source_extraction",
        "simple_autonomy": True,
        "simple_decision": decision.get("decision"),
        "simple_resolution": decision.get("simple_resolution"),
        "residue_kind": decision.get("residue_kind"),
        "affected_fact_count": 1,
        "reversible": True,
        "truth_mutation": False,
        "quote_backed": bool(candidate.get("source_spans") and candidate.get("evidence_quote")),
        "fallback_route": canonical_page_hint_for_fact(str(candidate.get("page_hint") or ""))
        in DEFAULT_FALLBACK_PAGE_HINTS,
    }


def find_exact_duplicate_fact(paths: BrainPaths, candidate: dict[str, Any]) -> dict[str, Any] | None:
    statement_key = normalized_statement_key(str(candidate.get("statement") or ""))
    if not statement_key:
        return None
    page_hint = candidate.get("page_hint")
    with connection(paths.sqlite_path) as conn:
        fact_rows = rows(
            conn,
            """
            SELECT *
            FROM facts
            WHERE status = 'active'
              AND COALESCE(page_hint, '') = COALESCE(?, '')
            ORDER BY observed_at DESC, created_at DESC
            """,
            (page_hint,),
        )
    for row in fact_rows:
        fact = row_to_fact(row)
        if normalized_statement_key(str(fact.get("statement") or "")) == statement_key:
            return fact
    return None


def merge_candidate_into_existing_fact(
    existing: dict[str, Any],
    candidate: dict[str, Any],
) -> dict[str, Any]:
    metadata = dict(existing.get("metadata") or {})
    metadata.setdefault("simple_autonomy", {})
    metadata["simple_autonomy"] = {
        **(metadata.get("simple_autonomy") if isinstance(metadata.get("simple_autonomy"), dict) else {}),
        "last_resolution": "exact_duplicate_source_union",
        "last_extractor_model": candidate.get("extractor_model"),
    }
    return {
        **existing,
        "source_ids": stable_unique_values(
            [*(existing.get("source_ids") or []), *(candidate.get("source_ids") or [])]
        ),
        "source_spans": stable_unique_dicts(
            [*(existing.get("source_spans") or []), *(candidate.get("source_spans") or [])]
        ),
        "evidence_quote": existing.get("evidence_quote") or candidate.get("evidence_quote"),
        "extraction_confidence": max_optional_float(
            existing.get("extraction_confidence"), candidate.get("extraction_confidence")
        ),
        "routing_confidence": max_optional_float(
            existing.get("routing_confidence"), candidate.get("routing_confidence")
        ),
        "truth_confidence": max_optional_float(
            existing.get("truth_confidence"), candidate.get("truth_confidence")
        )
        or existing.get("truth_confidence")
        or candidate.get("truth_confidence"),
        "metadata": metadata,
    }


def normalized_statement_key(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().lower())


def stable_unique_values(values: list[Any]) -> list[Any]:
    output: list[Any] = []
    seen: set[str] = set()
    for value in values:
        key = json.dumps(value, sort_keys=True, ensure_ascii=False)
        if key in seen:
            continue
        output.append(value)
        seen.add(key)
    return output


def stable_unique_dicts(values: list[Any]) -> list[dict[str, Any]]:
    return [item for item in stable_unique_values(values) if isinstance(item, dict)]


def max_optional_float(*values: Any) -> float | None:
    parsed = [optional_float(value) for value in values]
    present = [value for value in parsed if value is not None]
    return max(present) if present else None


def build_document_windows(
    document_id: str,
    chunks: list[dict[str, Any]],
    *,
    max_chunks: int,
    overlap_chunks: int,
) -> list[dict[str, Any]]:
    if not chunks:
        return []
    windows: list[dict[str, Any]] = []
    step = max(1, max_chunks - overlap_chunks)
    start = 0
    window_index = 0
    while start < len(chunks):
        end = min(len(chunks), start + max_chunks)
        window_chunks = chunks[start:end]
        windows.append(
            {
                "window_id": f"{document_id}:window:{window_index}",
                "window_index": window_index,
                "chunk_start_index": start,
                "chunk_end_index": end - 1,
                "chunks": window_chunks,
            }
        )
        if end >= len(chunks):
            break
        start += step
        window_index += 1
    return windows


def source_window_card(
    document: dict[str, Any],
    window: dict[str, Any],
    routing_hints: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "document": {
            "document_id": document["document_id"],
            "title": document["title"],
            "source_type": document["source_type"],
            "source_id": document["source_id"],
            "content_hash": document["content_hash"],
            "raw_content_hash": document.get("raw_content_hash"),
            "normalized_content_empty": bool(document.get("normalized_content_empty")),
        },
        "window": window,
        "routing_hints": routing_hints,
    }


def load_extraction_routing_hints(paths: BrainPaths, *, limit: int) -> list[dict[str, Any]]:
    if limit <= 0:
        return []
    with connection(paths.sqlite_path) as conn:
        contract_hints = [
            {
                "page_hint": row["page_hint"],
                "canonical_entity": row["canonical_entity"],
                "page_scope": row["page_scope"],
                "retrieval_purpose": row["retrieval_purpose"],
            }
            for row in conn.execute(
                """
                SELECT page_hint, canonical_entity, page_scope, retrieval_purpose
                FROM page_contracts
                WHERE status = 'active'
                ORDER BY COALESCE(updated_at, created_at) DESC, page_hint
                LIMIT ?
                """,
                (limit,),
            )
        ]
        if contract_hints:
            return contract_hints
        return [
            {
                "page_hint": row["path"],
                "canonical_entity": row["title"],
                "page_scope": row["page_type"],
                "retrieval_purpose": None,
            }
            for row in conn.execute(
                """
                SELECT path, title, page_type
                FROM wiki_pages
                WHERE status = 'active'
                ORDER BY updated_at DESC, path
                LIMIT ?
                """,
                (limit,),
            )
        ]


def record_extraction_watermarks(
    paths: BrainPaths,
    documents: list[dict[str, Any]],
    *,
    extractor_model: str | None,
    run_id: str | None,
    candidate_count: int,
    status: str = "ok",
    validation: dict[str, Any] | None = None,
) -> None:
    if not documents:
        return
    processed_at = now_iso()
    with connection(paths.sqlite_path) as conn:
        for document in documents:
            conn.execute(
                """
                INSERT INTO cos_stage_watermarks(
                  id, stage, document_id, content_hash, model, prompt_version,
                  status, run_id, processed_at, metadata
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(stage, document_id, content_hash, model, prompt_version)
                DO UPDATE SET
                  status = excluded.status,
                  run_id = excluded.run_id,
                  processed_at = excluded.processed_at,
                  metadata = excluded.metadata
                """,
                (
                    new_id("coswm"),
                    EXTRACTION_STAGE,
                    str(document["document_id"]),
                    str(document["content_hash"]),
                    extractor_model or "",
                    EXTRACTION_PROMPT_VERSION,
                    status,
                    run_id,
                    processed_at,
                    dumps(
                        {
                            "candidate_count": candidate_count,
                            "raw_content_hash": document.get("raw_content_hash"),
                            "normalized_content_hash": document.get("content_hash"),
                            "normalized_content_empty": bool(document.get("normalized_content_empty")),
                            "validation": validation or {},
                        }
                    ),
                ),
            )


def extraction_prompt(source_window: dict[str, Any]) -> str:
    return (
        "Extract atomic source-backed facts from this single source window.\n"
        "Each fact must include: statement, chunk_id, evidence_quote, page_hint, section_hint, "
        "claim_class, entity_key, extraction_confidence, routing_confidence, and truth_confidence.\n"
        "claim_class must be one of: decision, commitment, preference, role_or_responsibility, "
        "project_state, factual_update, open_question, event_metadata, transcript_mechanic, "
        "pleasantry, boilerplate, non_claim.\n"
        "Only propose durable claims worth future retrieval. Omit event metadata, transcript mechanics, "
        "pleasantries, boilerplate, and non-claims when possible. If you include one, label it accurately; "
        "deterministic policy will drop it.\n"
        "Evidence rules:\n"
        "- Use the exact chunk_id string from the source card.\n"
        "- evidence_quote must be an exact copied substring from that chunk text, not a paraphrase.\n"
        "- Copy evidence_quote character-for-character from the chunk text: preserve punctuation, spelling, "
        "line text, bullet prefixes, and spacing well enough for substring matching.\n"
        "- Do not use ellipses, bracketed edits, normalized wording, corrected typos, or stitched-together text "
        "from multiple locations inside evidence_quote.\n"
        "- Prefer a short exact quote over a long approximate quote.\n"
        "- Do not invent character offsets; deterministic code derives start/end from evidence_quote.\n"
        "- If you cannot quote source text for a fact, omit that fact.\n"
        "Routing rules:\n"
        "- page_hint must be a wiki-relative markdown path such as projects/example.md or concepts/example.md.\n"
        "- Do not use absolute file paths, raw source paths, or docs/*.md audit file paths as page_hint.\n\n"
        f"Source window JSON:\n{json.dumps(source_window, ensure_ascii=False, indent=2)}"
    )


def extraction_validation_retry_prompt(
    source_window: dict[str, Any],
    previous_response: dict[str, Any],
    validation_report: dict[str, Any],
) -> str:
    return (
        "The previous extractor response did not pass deterministic validation. "
        "Return a corrected full JSON object with a facts array containing only corrected or replacement "
        "facts for the failures. Drop facts that cannot be source-backed from the source cards.\n\n"
        "For every returned fact, evidence_quote must be copied verbatim from the named chunk. "
        "Do not paraphrase, add ellipses, fix transcript typos, or combine non-contiguous text.\n\n"
        "Validation failures JSON:\n"
        f"{json.dumps(compact_validation_report(validation_report), ensure_ascii=False, indent=2)}\n\n"
        "Previous failed facts JSON:\n"
        f"{json.dumps(failed_facts_from_response(previous_response, validation_report), ensure_ascii=False, indent=2)[:12000]}\n\n"
        "Do not repeat already accepted facts.\n\n"
        "Source window JSON:\n"
        f"{json.dumps(source_window, ensure_ascii=False, indent=2)}"
    )


def validate_extracted_facts(
    paths: BrainPaths,
    raw_facts: list[Any],
    *,
    extractor_model: str | None = None,
) -> list[dict[str, Any]]:
    return validate_extracted_facts_with_report(
        paths,
        raw_facts,
        extractor_model=extractor_model,
    )["candidates"]


def validate_extraction_payload(
    paths: BrainPaths,
    parsed: dict[str, Any],
    *,
    extractor_model: str | None = None,
) -> dict[str, Any]:
    raw_facts = parsed.get("facts")
    if not isinstance(raw_facts, list):
        report = empty_extraction_validation_report()
        report["schema_errors"].append("facts must be an array")
        report["rejected_count"] = 1
        return report
    return validate_extracted_facts_with_report(
        paths,
        raw_facts,
        extractor_model=extractor_model,
    )


def validate_extracted_facts_with_report(
    paths: BrainPaths,
    raw_facts: list[Any],
    *,
    extractor_model: str | None = None,
) -> dict[str, Any]:
    chunk_context_by_id = load_chunk_contexts(paths)
    candidates: list[dict[str, Any]] = []
    rejections: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    for index, item in enumerate(raw_facts):
        reasons: list[str] = []
        if not isinstance(item, dict):
            rejections.append(
                {
                    "index": index,
                    "statement": "",
                    "reasons": ["fact must be an object"],
                }
            )
            continue
        statement = str(item.get("statement") or "").strip()
        claim_class = normalize_claim_class(item.get("claim_class"))
        quote_refs = fact_quote_refs(item)
        if not statement:
            reasons.append("missing statement")
        if claim_class is None:
            reasons.append("missing claim_class")
        elif claim_class not in CLAIM_CLASSES:
            reasons.append(f"unknown claim_class: {clip_text(claim_class, 80)}")
        elif claim_class in NON_CLAIM_CLASSES:
            dropped.append(
                {
                    "index": index,
                    "statement": clip_text(statement),
                    "claim_class": claim_class,
                    "reason": "non_durable_claim_class",
                }
            )
            continue
        if not quote_refs:
            reasons.append("missing chunk_id/evidence_quote")
        if reasons:
            rejections.append(
                {
                    "index": index,
                    "statement": clip_text(statement),
                    "reasons": reasons,
                }
            )
            continue
        valid_spans = []
        source_ids = []
        valid_quotes = []
        quote_reasons: list[str] = []
        for quote_ref in quote_refs:
            chunk_id = quote_ref["chunk_id"]
            evidence_quote = quote_ref["evidence_quote"]
            if not chunk_id:
                quote_reasons.append("missing chunk_id")
                continue
            if not evidence_quote:
                quote_reasons.append(f"missing evidence_quote for {clip_text(chunk_id, 80)}")
                continue
            chunk_context = chunk_context_by_id.get(chunk_id)
            if chunk_context is None:
                quote_reasons.append(f"unknown chunk_id: {clip_text(chunk_id, 80)}")
                continue
            text = str(chunk_context["text"])
            quote_span = find_quote_span(text, evidence_quote)
            if quote_span is None:
                quote_reasons.append(
                    f"evidence_quote not found in {clip_text(chunk_id, 80)}: "
                    f"{clip_text(evidence_quote)}"
                )
                continue
            local_start, local_end = quote_span
            valid_spans.append({"chunk_id": chunk_id, "start": local_start, "end": local_end})
            valid_quotes.append(evidence_quote)
            source_ids.append(f"chunk:{chunk_id}")
        if not valid_spans:
            rejections.append(
                {
                    "index": index,
                    "statement": clip_text(statement),
                    "reasons": [*reasons, *(quote_reasons or ["no valid evidence_quote"])],
                }
            )
            continue
        page_hint = canonical_page_hint_for_fact(str(item.get("page_hint") or "concepts/extracted-facts.md"))
        section_hint = str(item.get("section_hint") or "Summary")
        model_entity_key = str(item.get("entity_key") or "").strip()
        entity_key = entity_key_for_change(topic_for_path(page_hint), page_hint, section_hint)
        candidates.append(
            {
                "statement": statement,
                "entity_key": entity_key,
                "page_hint": page_hint,
                "section_hint": section_hint,
                "claim_class": claim_class,
                "source_ids": sorted(set(source_ids)),
                "source_spans": valid_spans,
                "evidence_quote": valid_quotes[0][:1000] if valid_quotes else None,
                "observed_at": item.get("observed_at") or now_iso(),
                "effective_at": item.get("effective_at"),
                "confidence": float(item.get("truth_confidence") or item.get("confidence") or 0.5),
                "extraction_confidence": optional_float(item.get("extraction_confidence")),
                "routing_confidence": optional_float(item.get("routing_confidence")),
                "truth_confidence": optional_float(item.get("truth_confidence")) or float(item.get("confidence") or 0.5),
                "extraction_method": "llm",
                "extractor_model": item.get("extractor_model") or extractor_model,
                "metadata": {
                    "source": "source_to_facts_extraction",
                    "claim_class": claim_class,
                    **({"model_entity_key": model_entity_key} if model_entity_key else {}),
                },
            }
        )
    return {
        "candidates": candidates,
        "raw_fact_count": len(raw_facts),
        "accepted_count": len(candidates),
        "rejected_count": len(rejections),
        "dropped_count": len(dropped),
        "schema_errors": [],
        "rejections": rejections,
        "dropped": dropped,
    }


def fact_quote_refs(item: dict[str, Any]) -> list[dict[str, str]]:
    refs: list[dict[str, str]] = []
    fact_quote = str(item.get("evidence_quote") or "").strip()
    fact_chunk_id = str(item.get("chunk_id") or "").strip()
    if fact_chunk_id or fact_quote:
        refs.append({"chunk_id": fact_chunk_id, "evidence_quote": fact_quote})
    for span in item.get("source_spans") or []:
        if not isinstance(span, dict):
            continue
        chunk_id = str(span.get("chunk_id") or "").strip()
        evidence_quote = str(span.get("evidence_quote") or fact_quote).strip()
        if chunk_id or evidence_quote:
            refs.append({"chunk_id": chunk_id, "evidence_quote": evidence_quote})
    return refs


def find_quote_span(text: str, quote: str) -> tuple[int, int] | None:
    stripped = quote.strip()
    if not stripped:
        return None
    exact_start = text.find(stripped)
    if exact_start >= 0:
        return exact_start, exact_start + len(stripped)
    normalized_text, index_map = normalize_with_index_map(text)
    normalized_quote, _ = normalize_with_index_map(stripped)
    if not normalized_quote:
        return None
    normalized_start = normalized_text.find(normalized_quote)
    if normalized_start < 0:
        return None
    normalized_end = normalized_start + len(normalized_quote) - 1
    if normalized_start >= len(index_map) or normalized_end >= len(index_map):
        return None
    return index_map[normalized_start], index_map[normalized_end] + 1


def normalize_with_index_map(value: str) -> tuple[str, list[int]]:
    output: list[str] = []
    index_map: list[int] = []
    in_whitespace = False
    for index, char in enumerate(value):
        if char.isspace():
            if output and not in_whitespace:
                output.append(" ")
                index_map.append(index)
            in_whitespace = True
            continue
        output.append(char)
        index_map.append(index)
        in_whitespace = False
    if output and output[-1] == " ":
        output.pop()
        index_map.pop()
    return "".join(output), index_map


def failed_facts_from_response(
    previous_response: dict[str, Any],
    validation_report: dict[str, Any],
) -> list[Any]:
    facts = previous_response.get("facts")
    if not isinstance(facts, list):
        return []
    failed = []
    for rejection in validation_report.get("rejections") or []:
        index = rejection.get("index")
        if isinstance(index, int) and 0 <= index < len(facts):
            failed.append(facts[index])
    return failed


def candidate_dedupe_key(candidate: dict[str, Any]) -> tuple[Any, ...]:
    spans = tuple(
        (
            span.get("chunk_id"),
            span.get("start"),
            span.get("end"),
        )
        for span in candidate.get("source_spans") or []
    )
    return (candidate.get("statement"), candidate.get("page_hint"), spans)


def aggregate_document_validation(
    document: dict[str, Any],
    window_validations: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "document_id": document["document_id"],
        "source_type": document["source_type"],
        "window_count": len(window_validations),
        "raw_fact_count": sum(int(item.get("raw_fact_count") or 0) for item in window_validations),
        "accepted_count": len(candidates),
        "rejected_count": sum(int(item.get("rejected_count") or 0) for item in window_validations),
        "dropped_count": sum(int(item.get("dropped_count") or 0) for item in window_validations),
        "total_rejected_count": sum(
            int(item.get("total_rejected_count") or item.get("rejected_count") or 0)
            for item in window_validations
        ),
        "schema_errors": [
            error for item in window_validations for error in (item.get("schema_errors") or [])
        ],
        "rejections": [
            rejection for item in window_validations for rejection in (item.get("rejections") or [])
        ][:8],
        "dropped": [
            dropped for item in window_validations for dropped in (item.get("dropped") or [])
        ][:8],
        "windows": window_validations,
    }


def aggregate_run_validation(
    document_validations: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "document_count": len(document_validations),
        "raw_fact_count": sum(int(item.get("raw_fact_count") or 0) for item in document_validations),
        "accepted_count": len(candidates),
        "rejected_count": sum(int(item.get("rejected_count") or 0) for item in document_validations),
        "dropped_count": sum(int(item.get("dropped_count") or 0) for item in document_validations),
        "total_rejected_count": sum(
            int(item.get("total_rejected_count") or item.get("rejected_count") or 0)
            for item in document_validations
        ),
        "schema_errors": [
            error for item in document_validations for error in (item.get("schema_errors") or [])
        ],
        "rejections": [
            rejection for item in document_validations for rejection in (item.get("rejections") or [])
        ][:8],
        "dropped": [
            dropped for item in document_validations for dropped in (item.get("dropped") or [])
        ][:8],
    }


def empty_extraction_validation_report() -> dict[str, Any]:
    return {
        "candidates": [],
        "raw_fact_count": 0,
        "accepted_count": 0,
        "rejected_count": 0,
        "dropped_count": 0,
        "schema_errors": [],
        "rejections": [],
        "dropped": [],
    }


def compact_validation_report(
    report: dict[str, Any],
    *,
    attempt: int | None = None,
) -> dict[str, Any]:
    output: dict[str, Any] = {
        "raw_fact_count": int(report.get("raw_fact_count") or 0),
        "accepted_count": int(report.get("accepted_count") or 0),
        "rejected_count": int(report.get("rejected_count") or 0),
        "dropped_count": int(report.get("dropped_count") or 0),
        "schema_errors": list(report.get("schema_errors") or []),
        "rejections": list(report.get("rejections") or [])[:8],
        "dropped": list(report.get("dropped") or [])[:8],
    }
    if attempt is not None:
        output["attempt"] = attempt
    return output


def better_extraction_report(candidate: dict[str, Any], current: dict[str, Any]) -> bool:
    candidate_score = (
        int(candidate.get("accepted_count") or 0),
        -int(candidate.get("rejected_count") or 0),
        -len(candidate.get("schema_errors") or []),
    )
    current_score = (
        int(current.get("accepted_count") or 0),
        -int(current.get("rejected_count") or 0),
        -len(current.get("schema_errors") or []),
    )
    return candidate_score > current_score


def extraction_needs_validation_retry(report: dict[str, Any]) -> bool:
    return bool(report.get("schema_errors") or int(report.get("rejected_count") or 0) > 0)


def selected_validation_attempt(
    attempts: list[dict[str, Any]],
    selected: dict[str, Any],
) -> int | None:
    selected_score = (
        int(selected.get("accepted_count") or 0),
        -int(selected.get("rejected_count") or 0),
        -len(selected.get("schema_errors") or []),
    )
    for attempt in attempts:
        attempt_score = (
            int(attempt.get("accepted_count") or 0),
            -int(attempt.get("rejected_count") or 0),
            -len(attempt.get("schema_errors") or []),
        )
        if attempt_score == selected_score:
            return int(attempt["attempt"])
    return None


def extraction_watermark_status(
    validation: dict[str, Any],
    candidates: list[dict[str, Any]],
) -> str:
    if candidates:
        return "ok"
    if validation.get("schema_errors") or int(validation.get("rejected_count") or 0) > 0:
        return "invalid"
    return "extracted_empty"


def normalize_claim_class(value: Any) -> str | None:
    normalized = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    return normalized or None


def normalized_extraction_content_hash(chunks: list[dict[str, Any]]) -> str:
    normalized = normalized_extraction_content(chunks)
    return f"normalized:{hashlib.sha256(normalized.encode('utf-8')).hexdigest()}"


def normalized_extraction_content(chunks: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for chunk in chunks:
        for raw_line in str(chunk.get("text") or "").splitlines():
            line = re.sub(r"\s+", " ", raw_line.strip())
            if not line:
                continue
            if low_information_line(line):
                continue
            lines.append(line.lower())
    return "\n".join(lines)


def low_information_line(line: str) -> bool:
    return any(pattern.match(line) for pattern in LOW_INFORMATION_LINE_PATTERNS)


def load_chunk_contexts(paths: BrainPaths) -> dict[str, dict[str, Any]]:
    with connection(paths.sqlite_path) as conn:
        return {
            str(row["id"]): {
                "text": str(row["text"] or ""),
            }
            for row in conn.execute("SELECT id, text FROM chunks")
        }


def clip_text(value: str, limit: int = 160) -> str:
    return value if len(value) <= limit else f"{value[: limit - 3]}..."


def optional_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
