from __future__ import annotations

import hashlib
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from difflib import SequenceMatcher
from typing import Any

from .cos_actions import (
    apply_action,
    get_action,
    mark_action_residue,
    mark_simple_autonomy_applied,
    propose_action,
    refresh_action_residue_question,
)
from .db import connection, dumps, rows
from .entities import (
    ENTITY_TYPES,
    MENTION_KINDS,
    normalize_entity_name,
    normalize_entity_type,
    normalize_mention_kind,
    resolve_entity,
)
from .fact_relations import classify_fact_relation
from .llm import (
    LLMProvider,
    LLMProviderError,
    complete_json,
    cos_role_provider_configured,
    get_cos_role_provider,
    load_cos_llm_config,
)
from .paths import BrainPaths
from .policy_action_batch import decide_policy_actions
from .routing_coherence import (
    coherence_bonus,
    fact_document_id,
    load_document_route_priors,
    route_priors_from_facts,
    strong_document_prior,
)
from .source_evidence import (
    evidence_units_for_text,
    extraction_confidence_values,
    resolve_evidence_unit_ids,
)
from .source_dates import document_source_date_metadata, stamp_candidate_source_context
from .unrouted_resolution import (
    candidate_requires_route_resolution,
    candidate_route_metadata,
    fact_route_reclaim_query,
    reclaim_route_record,
    resolve_unrouted_candidate_routes,
)
from .util import new_id, now_iso
from .wiki import NON_ROUTABLE_PAGE_TYPES
from .wiki_facts import (
    canonical_page_hint_for_fact,
    entity_key_for_change,
    facts_directly_conflict,
    indexed_page_relative_path,
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
                "required": [
                    "statement",
                    "chunk_id",
                    "evidence_unit_ids",
                    "claim_class",
                    "extraction_confidence",
                    "routing_confidence",
                    "truth_confidence",
                ],
            },
        }
    },
}
CONFLICT_PRECHECK_SCHEMA = {
    "type": "object",
    "required": ["decision", "rationale"],
    "properties": {
        "decision": {"type": "string", "enum": ["conflict", "no_conflict"]},
        "counterpart_fact_ids": {"type": "array", "items": {"type": "string"}},
        "rationale": {"type": "string"},
    },
}
EXTRACTION_PROMPT_VERSION = "extractor-evidence-units-v6-speaker-context"
COMPATIBLE_EXTRACTION_PROMPT_VERSIONS = ("extractor-evidence-units-v5",)
EXTRACTION_STAGE = "extractor"
EXTRACTION_VALIDATION_ATTEMPTS = 2
MAX_EVIDENCE_UNITS_PER_FACT = 5
DEFAULT_EXTRACTION_WINDOW_CHUNKS = 6
DEFAULT_EXTRACTION_WINDOW_OVERLAP_CHUNKS = 1
DEFAULT_EXTRACTION_MAX_WORKERS = 1
MAX_EXTRACTION_MAX_WORKERS = 16
DEFAULT_CRITIC_REVIEW_MAX_WORKERS = 4
DEFAULT_CRITIC_REVIEW_TIMEOUT_SECONDS = 300
DEFAULT_CRITIC_BLOCK_RATE_ANOMALY_THRESHOLD = 0.8
DEFAULT_CRITIC_BLOCK_RATE_ANOMALY_MIN_REVIEWED = 5
DEFAULT_ROUTING_HINT_LIMIT = 80
ROUTING_HINT_POOL_LIMIT = 2000
DEFAULT_SKIPPED_SOURCE_TYPES = {"agent_session_log"}
DEFAULT_FALLBACK_PAGE_HINTS = {"concepts/extracted-facts.md"}
REFERENCE_ROUTE_PREFIXES = ("references/", "wiki/references/")
INVALID_ROUTE_PREFIXES = (
    *REFERENCE_ROUTE_PREFIXES,
    "docs/",
    "raw/",
    "inbox/",
    "db/",
    "config/",
)
INVALID_ROUTE_PAGE_TYPES = NON_ROUTABLE_PAGE_TYPES
CANONICAL_ROUTE_NAMESPACES = {
    "career",
    "companies",
    "concepts",
    "decisions",
    "events",
    "ideas",
    "open_loops",
    "people",
    "products",
    "projects",
}
ROUTE_FUZZY_DUP_THRESHOLD = 0.92
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
    re.compile(r"^#{1,6}\s+.*$"),
    re.compile(r"^title\s*:\s*.+$", re.IGNORECASE),
    re.compile(
        r"^(source_type|agent|source_updated_at|location|participants|"
        r"transcript_render_version)\s*:\s*.*$",
        re.IGNORECASE,
    ),
    re.compile(r"^(event_)?(started|ended)_at\s*:\s*.+$", re.IGNORECASE),
    re.compile(r"^(created|updated|captured|ingested)_at\s*:\s*.+$", re.IGNORECASE),
    re.compile(r"^source_path\s*:\s*.+$", re.IGNORECASE),
    re.compile(r"^session_id\s*:\s*.+$", re.IGNORECASE),
    re.compile(r"^no\s+(summary|memo|transcript)\s+was\s+captured\.?$", re.IGNORECASE),
    re.compile(
        r"^\[transcript note:\s*source timestamps use overlapping speaker-track clocks;.*\]$",
        re.IGNORECASE,
    ),
    re.compile(
        r"^(?:the\s+)?(?:hyprnote\s+)?(?:meeting\s+)?"
        r"(?:document|session|record)\b.*\bhas\s+no\s+captured\s+"
        r"(summary|memo|transcript)\.?$",
        re.IGNORECASE,
    ),
    re.compile(
        r"^\d{4}-\d{2}-\d{2}t\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:z|[+-]\d{2}:\d{2})?$",
        re.IGNORECASE,
    ),
)
LOW_VALUE_FACT_STATEMENT_PATTERNS = (
    re.compile(
        r"^no\s+(summary|memo|transcript)\s+was\s+captured(?:\s+for\b.*)?\.?$",
        re.IGNORECASE,
    ),
    re.compile(
        r"^(?:the\s+)?(?:hyprnote\s+)?(?:meeting\s+)?"
        r"(?:document|session|record)\b.*\bhas\s+no\s+captured\s+"
        r"(summary|memo|transcript)\.?$",
        re.IGNORECASE,
    ),
)


def extract_recent_documents(
    paths: BrainPaths,
    *,
    limit: int = 10,
    offset: int = 0,
    shadow: bool = True,
    llm_provider: LLMProvider | None = None,
    provider: str | None = None,
    changed_only: bool = True,
    run_id: str | None = None,
    max_workers: int | None = None,
    critic_disagreement_mode: str | None = None,
    critic_max_workers: int | None = None,
    critic_timeout_seconds: int | None = None,
    source_types: list[str] | None = None,
    document_ids: list[str] | None = None,
) -> dict[str, Any]:
    run_started = time.perf_counter()
    if not cos_role_provider_configured(
        paths, "extractor", llm_provider=llm_provider, provider=provider
    ):
        return {
            "status": "skipped",
            "reason": "No CoS LLM provider configured for extractor role",
            "shadow": shadow,
            "documents": [],
            "candidates": [],
            "actions": [],
            "timing": extraction_run_timing(run_started),
        }
    active_provider = get_cos_role_provider(
        paths, "extractor", provider=provider, llm_provider=llm_provider
    )
    extractor_model = getattr(active_provider, "model", None)
    selection_started = time.perf_counter()
    extraction_config = load_extraction_config(paths)
    worker_count = normalize_extraction_max_workers(
        max_workers if max_workers is not None else extraction_config.get("max_workers")
    )
    critic_review = critic_review_config(
        extraction_config,
        disagreement_mode=critic_disagreement_mode,
        max_workers=critic_max_workers,
        timeout_seconds=critic_timeout_seconds,
    )
    source_type_filter = {
        str(item).strip() for item in source_types or [] if str(item).strip()
    }
    document_id_filter = {
        str(item).strip() for item in document_ids or [] if str(item).strip()
    }
    selection_limit = (
        max(1, limit)
        if offset <= 0 and not source_type_filter and not document_id_filter
        else 10_000_000
    )
    documents = recent_source_cards(
        paths,
        limit=selection_limit,
        changed_only=changed_only,
        extractor_model=extractor_model,
        prompt_version=EXTRACTION_PROMPT_VERSION,
        extraction_config=extraction_config,
    )
    if source_type_filter:
        documents = [
            document
            for document in documents
            if str(document.get("source_type") or "") in source_type_filter
        ]
    if document_id_filter:
        documents = [
            document
            for document in documents
            if str(document.get("document_id") or "") in document_id_filter
        ]
    if offset > 0:
        documents = documents[offset:]
    documents = documents[: max(0, limit)]
    selection_duration_ms = elapsed_ms(selection_started)
    if not documents:
        timing = extraction_run_timing(
            run_started,
            selection_duration_ms=selection_duration_ms,
            worker_count=worker_count,
        )
        return {
            "status": "ok",
            "shadow": shadow,
            "documents": [],
            "candidates": [],
            "actions": [],
            "timing": timing,
        }
    routing_started = time.perf_counter()
    routing_hint_limit = int(
        extraction_config.get("routing_hints_limit") or DEFAULT_ROUTING_HINT_LIMIT
    )
    routing_hint_pool = load_extraction_routing_hint_pool(paths)
    routing_duration_ms = elapsed_ms(routing_started)
    extraction_started = time.perf_counter()
    document_outputs = extract_document_windows(
        paths,
        documents,
        routing_hint_pool,
        routing_hint_limit=routing_hint_limit,
        provider=provider,
        llm_provider=active_provider,
        extractor_model=extractor_model,
        max_workers=worker_count,
    )
    extraction_duration_ms = elapsed_ms(extraction_started)
    candidates: list[dict[str, Any]] = []
    document_validations: list[dict[str, Any]] = []
    route_targets = load_extraction_route_targets(paths)
    for index, document in enumerate(documents):
        document_candidates = apply_document_route_coherence(
            document_outputs[index]["candidates"], route_targets
        )
        window_validations = document_outputs[index]["window_validations"]
        document_validation = aggregate_document_validation(
            document, window_validations, document_candidates
        )
        record_extraction_watermarks(
            paths,
            [document],
            extractor_model=extractor_model,
            run_id=run_id,
            candidate_count=len(document_candidates),
            status=extraction_watermark_status(
                document_validation, document_candidates
            ),
            validation=document_validation,
        )
        candidates.extend(document_candidates)
        document_validations.append(document_validation)
    validation = aggregate_run_validation(document_validations, candidates)
    actions: list[dict[str, Any]] = []
    apply_duration_ms = 0.0
    if not shadow:
        apply_started = time.perf_counter()
        simple_autonomy = simple_autonomy_config(extraction_config)
        if simple_autonomy["enabled"]:
            actions = apply_simple_autonomy_candidates(
                paths,
                candidates,
                run_id=run_id,
                simple_autonomy=simple_autonomy,
            )
        else:
            actions = propose_policy_gated_candidates(
                paths,
                candidates,
                run_id=run_id,
                critic_review=critic_review,
            )
        apply_duration_ms = elapsed_ms(apply_started)
    timing = extraction_run_timing(
        run_started,
        selection_duration_ms=selection_duration_ms,
        routing_hints_duration_ms=routing_duration_ms,
        extraction_duration_ms=extraction_duration_ms,
        apply_duration_ms=apply_duration_ms,
        worker_count=worker_count,
    )
    validation["duration_ms"] = timing["duration_ms"]
    validation["selection_duration_ms"] = timing["selection_duration_ms"]
    validation["routing_hints_duration_ms"] = timing["routing_hints_duration_ms"]
    validation["extraction_duration_ms"] = timing["extraction_duration_ms"]
    validation["apply_duration_ms"] = timing["apply_duration_ms"]
    validation["worker_count"] = timing["worker_count"]
    return {
        "status": "ok",
        "shadow": shadow,
        "documents": documents,
        "candidates": candidates,
        "actions": actions,
        "validation": validation,
        "document_validations": document_validations,
        "timing": timing,
    }


def extract_document_windows(
    paths: BrainPaths,
    documents: list[dict[str, Any]],
    routing_hint_pool: list[dict[str, Any]],
    *,
    routing_hint_limit: int,
    provider: str | None,
    llm_provider: LLMProvider,
    extractor_model: str | None,
    max_workers: int,
) -> list[dict[str, Any]]:
    outputs = [{"candidates": [], "window_validations": []} for _ in documents]
    jobs: list[tuple[int, int, dict[str, Any], dict[str, Any]]] = []
    for document_index, document in enumerate(documents):
        for window_index, window in enumerate(document.get("windows") or []):
            jobs.append((document_index, window_index, document, window))
    if not jobs:
        return outputs
    worker_count = min(max(1, max_workers), len(jobs))
    if worker_count == 1:
        results = [
            extract_window_job_safely(
                paths,
                job,
                routing_hint_pool=routing_hint_pool,
                routing_hint_limit=routing_hint_limit,
                provider=provider,
                llm_provider=llm_provider,
                extractor_model=extractor_model,
            )
            for job in jobs
        ]
    else:
        results = []
        with ThreadPoolExecutor(
            max_workers=worker_count, thread_name_prefix="brain-extract"
        ) as executor:
            futures = [
                executor.submit(
                    extract_window_job_safely,
                    paths,
                    job,
                    routing_hint_pool=routing_hint_pool,
                    routing_hint_limit=routing_hint_limit,
                    provider=provider,
                    llm_provider=llm_provider,
                    extractor_model=extractor_model,
                )
                for job in jobs
            ]
            for future in as_completed(futures):
                results.append(future.result())
    for result in sorted(
        results, key=lambda item: (item["document_index"], item["window_index"])
    ):
        output = outputs[result["document_index"]]
        output["candidates"].extend(result["candidates"])
        output["window_validations"].append(result["window_validation"])
    return outputs


def extract_window_job_safely(
    paths: BrainPaths,
    job: tuple[int, int, dict[str, Any], dict[str, Any]],
    **kwargs: Any,
) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        return extract_window_job(paths, job, **kwargs)
    except LLMProviderError as exc:
        document_index, window_index, _document, window = job
        message = str(exc)[:1000]
        return {
            "document_index": document_index,
            "window_index": window_index,
            "candidates": [],
            "window_validation": {
                "window_id": window["window_id"],
                "window_index": window.get("window_index"),
                "chunk_ids": [
                    chunk["chunk_id"] for chunk in window.get("chunks") or []
                ],
                "routing_hint_count": 0,
                "routing_hint_page_hints": [],
                "raw_fact_count": 0,
                "accepted_count": 0,
                "rejected_count": 0,
                "dropped_count": 0,
                "attempt_count": 0,
                "duration_ms": elapsed_ms(started),
                "schema_errors": [f"extractor_provider_error: {message}"],
                "rejections": [],
                "dropped": [],
                "provider_error": message,
            },
        }


def extract_window_job(
    paths: BrainPaths,
    job: tuple[int, int, dict[str, Any], dict[str, Any]],
    *,
    routing_hint_pool: list[dict[str, Any]],
    routing_hint_limit: int,
    provider: str | None,
    llm_provider: LLMProvider,
    extractor_model: str | None,
) -> dict[str, Any]:
    document_index, window_index, document, window = job
    routing_hints = ranked_extraction_routing_hints(
        routing_hint_pool,
        routing_query_text(document, window),
        limit=routing_hint_limit,
    )
    extraction_result = extract_facts_with_validation_retry(
        paths,
        source_window_card(document, window, routing_hints),
        provider=provider,
        llm_provider=llm_provider,
        extractor_model=extractor_model,
    )
    for candidate in extraction_result["candidates"]:
        stamp_candidate_source_context(candidate, document, window["window_id"])
    return {
        "document_index": document_index,
        "window_index": window_index,
        "candidates": extraction_result["candidates"],
        "window_validation": {
            "window_id": window["window_id"],
            "window_index": window.get("window_index"),
            "chunk_ids": [chunk["chunk_id"] for chunk in window.get("chunks") or []],
            "routing_hint_count": len(routing_hints),
            "routing_hint_page_hints": [
                str(hint.get("page_hint") or "") for hint in routing_hints
            ],
            **extraction_result["validation"],
        },
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
    extraction_started = time.perf_counter()
    prompt = extraction_prompt(source_window)
    initial_prompt_char_count = len(prompt)
    source_window_char_count = len(json.dumps(source_window, ensure_ascii=False))
    accepted: list[dict[str, Any]] = []
    accepted_keys: set[tuple[Any, ...]] = set()
    best_report: dict[str, Any] | None = None
    attempts: list[dict[str, Any]] = []
    last_report = empty_extraction_validation_report()
    total_proposed_count = 0
    total_rejected_count = 0
    total_dropped_count = 0
    accepted_from_retry_count = 0
    total_prompt_char_count = 0
    for attempt_index in range(max_validation_attempts):
        attempt_started = time.perf_counter()
        attempt_prompt_char_count = len(prompt)
        total_prompt_char_count += attempt_prompt_char_count
        llm_started = time.perf_counter()
        parsed = complete_json(
            prompt,
            schema=EXTRACTION_SCHEMA,
            provider=provider,
            role="extractor",
            llm_provider=llm_provider,
            paths=paths,
        )
        llm_duration_ms = elapsed_ms(llm_started)
        validation_started = time.perf_counter()
        report = validate_extraction_payload(
            paths, parsed, extractor_model=extractor_model
        )
        validation_duration_ms = elapsed_ms(validation_started)
        report["duration_ms"] = elapsed_ms(attempt_started)
        report["llm_duration_ms"] = llm_duration_ms
        report["validation_duration_ms"] = validation_duration_ms
        report["prompt_char_count"] = attempt_prompt_char_count
        report["source_window_char_count"] = source_window_char_count
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
    validation["attempt_count"] = len(attempts)
    validation["duration_ms"] = elapsed_ms(extraction_started)
    validation["llm_duration_ms"] = round(
        sum(float(attempt.get("llm_duration_ms") or 0.0) for attempt in attempts), 3
    )
    validation["validation_duration_ms"] = round(
        sum(
            float(attempt.get("validation_duration_ms") or 0.0) for attempt in attempts
        ),
        3,
    )
    validation["initial_prompt_char_count"] = initial_prompt_char_count
    validation["prompt_char_count"] = total_prompt_char_count
    validation["source_window_char_count"] = source_window_char_count
    validation["selected_attempt"] = selected_validation_attempt(
        attempts, compact_validation_report(best_report)
    )
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
    window_max_chunks = int(
        config.get("window_max_chunks") or DEFAULT_EXTRACTION_WINDOW_CHUNKS
    )
    window_overlap_chunks = int(
        config.get("window_overlap_chunks") or DEFAULT_EXTRACTION_WINDOW_OVERLAP_CHUNKS
    )
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
            policy = extraction_policy_for_source_type(
                config, str(document["source_type"])
            )
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
            windows: list[dict[str, Any]] = []
            skipped_windows: list[dict[str, Any]] = []
            if not normalized_content:
                skipped_windows.append(
                    skipped_extraction_window(
                        str(document["id"]),
                        chunks,
                        reason="normalized_content_empty",
                    )
                )
            else:
                for window in build_document_windows(
                    str(document["id"]),
                    chunks,
                    max_chunks=window_max_chunks,
                    overlap_chunks=window_overlap_chunks,
                ):
                    reason = extraction_window_prefilter_reason(window)
                    if reason:
                        skipped_windows.append(
                            skipped_extraction_window_from_window(window, reason=reason)
                        )
                        continue
                    windows.append(window)
            output.append(
                {
                    "document_id": document["id"],
                    "title": document["title"],
                    "source_type": document["source_type"],
                    "source_id": f"document:{document['id']}",
                    **document_source_date_metadata(dict(document)),
                    "content_hash": normalized_content_hash,
                    "raw_content_hash": document["content_hash"],
                    "normalized_content_empty": not bool(normalized_content),
                    "policy": policy,
                    "chunks": chunks,
                    "windows": windows,
                    "skipped_windows": skipped_windows,
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
    prompt_versions = [prompt_version]
    if prompt_version == EXTRACTION_PROMPT_VERSION:
        prompt_versions.extend(COMPATIBLE_EXTRACTION_PROMPT_VERSIONS)
    prompt_placeholders = ",".join("?" for _ in prompt_versions)
    row = conn.execute(
        f"""
        SELECT 1
        FROM cos_stage_watermarks
        WHERE stage = ?
          AND document_id = ?
          AND content_hash = ?
          AND COALESCE(model, '') = COALESCE(?, '')
          AND prompt_version IN ({prompt_placeholders})
          AND status IN (?, ?)
        LIMIT 1
        """,
        (
            EXTRACTION_STAGE,
            document_id,
            content_hash,
            extractor_model,
            *prompt_versions,
            *sorted(TERMINAL_EXTRACTION_WATERMARK_STATUSES),
        ),
    ).fetchone()
    return row is not None


def load_extraction_config(paths: BrainPaths) -> dict[str, Any]:
    raw_config = load_cos_llm_config(paths).get("extraction")
    raw = raw_config if isinstance(raw_config, dict) else {}
    window = raw.get("window") if isinstance(raw.get("window"), dict) else {}
    source_types = (
        raw.get("source_types") if isinstance(raw.get("source_types"), dict) else {}
    )
    parallelism = (
        raw.get("parallelism") if isinstance(raw.get("parallelism"), dict) else {}
    )
    simple_autonomy = normalize_simple_autonomy_config(raw.get("simple_autonomy"))
    critic_review = normalize_critic_review_config(raw.get("critic_review"))
    max_chunks = max(
        1, int(window.get("max_chunks") or DEFAULT_EXTRACTION_WINDOW_CHUNKS)
    )
    overlap_chunks = max(
        0, int(window.get("overlap_chunks") or DEFAULT_EXTRACTION_WINDOW_OVERLAP_CHUNKS)
    )
    if overlap_chunks >= max_chunks:
        overlap_chunks = max(0, max_chunks - 1)
    return {
        "source_types": source_types,
        "window_max_chunks": max_chunks,
        "window_overlap_chunks": overlap_chunks,
        "routing_hints_limit": max(
            0, int(raw.get("routing_hints_limit") or DEFAULT_ROUTING_HINT_LIMIT)
        ),
        "max_workers": normalize_extraction_max_workers(
            raw.get("max_workers")
            or raw.get("window_workers")
            or parallelism.get("max_workers")
            or parallelism.get("window_workers")
            or DEFAULT_EXTRACTION_MAX_WORKERS
        ),
        "simple_autonomy": simple_autonomy,
        "critic_review": critic_review,
    }


def extraction_policy_for_source_type(
    config: dict[str, Any], source_type: str
) -> dict[str, Any]:
    source_types = (
        config.get("source_types")
        if isinstance(config.get("source_types"), dict)
        else {}
    )
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


def normalize_extraction_max_workers(value: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = DEFAULT_EXTRACTION_MAX_WORKERS
    return min(MAX_EXTRACTION_MAX_WORKERS, max(1, parsed))


def normalize_critic_review_config(value: Any) -> dict[str, Any]:
    raw = value if isinstance(value, dict) else {}
    return {
        "max_workers": normalize_extraction_max_workers(
            raw.get("max_workers")
            or raw.get("workers")
            or DEFAULT_CRITIC_REVIEW_MAX_WORKERS
        ),
        "timeout_seconds": normalize_positive_int(
            raw.get("timeout_seconds"), DEFAULT_CRITIC_REVIEW_TIMEOUT_SECONDS
        ),
        "disagreement_mode": normalize_critic_disagreement_mode(
            raw.get("disagreement_mode")
        ),
        "block_rate_anomaly_threshold": normalize_threshold(
            raw.get("block_rate_anomaly_threshold"),
            DEFAULT_CRITIC_BLOCK_RATE_ANOMALY_THRESHOLD,
        ),
        "block_rate_anomaly_min_reviewed": normalize_positive_int(
            raw.get("block_rate_anomaly_min_reviewed"),
            DEFAULT_CRITIC_BLOCK_RATE_ANOMALY_MIN_REVIEWED,
        ),
    }


def default_critic_review_config() -> dict[str, Any]:
    return normalize_critic_review_config({})


def critic_review_config(
    config: dict[str, Any],
    *,
    disagreement_mode: str | None,
    max_workers: int | None,
    timeout_seconds: int | None,
) -> dict[str, Any]:
    review = dict(config.get("critic_review") or default_critic_review_config())
    if disagreement_mode is not None:
        review["disagreement_mode"] = normalize_critic_disagreement_mode(
            disagreement_mode
        )
    if max_workers is not None:
        review["max_workers"] = normalize_extraction_max_workers(max_workers)
    if timeout_seconds is not None:
        review["timeout_seconds"] = normalize_positive_int(
            timeout_seconds, DEFAULT_CRITIC_REVIEW_TIMEOUT_SECONDS
        )
    return review


def normalize_critic_disagreement_mode(value: Any) -> str:
    mode = str(value or "needs_human").strip().lower().replace("-", "_")
    return mode if mode in {"needs_human", "reject"} else "needs_human"


def normalize_positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(1, parsed)


def normalize_threshold(value: Any, default: float) -> float | None:
    if value is None:
        return default
    parsed = optional_float(value)
    if parsed is None:
        return default
    if parsed <= 0:
        return None
    return min(1.0, parsed)


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
        "min_extraction_confidence": optional_float(
            raw.get("min_extraction_confidence")
        ),
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
    critic_review: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    candidates = resolve_unrouted_candidate_routes(
        paths, candidates, load_extraction_route_targets(paths)
    )
    actions: list[dict[str, Any]] = []
    pending_decisions: list[tuple[int, str]] = []
    review = critic_review or default_critic_review_config()
    for candidate in candidates:
        decision = earned_fact_decision(paths, candidate)
        action_fact = (
            decision.get("fact")
            if isinstance(decision.get("fact"), dict)
            else candidate
        )
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
            evidence=decision.get("evidence")
            if isinstance(decision.get("evidence"), dict)
            else None,
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
        pending_decisions.append((len(actions), action["id"]))
        actions.append(action)
    if pending_decisions:
        decided = decide_policy_actions(
            paths,
            [action_id for _, action_id in pending_decisions],
            critic_review=review,
        )
        for (index, _action_id), decided_action in zip(pending_decisions, decided):
            actions[index] = decided_action
    if review.get("block_rate_anomaly_threshold") is not None:
        record_critic_block_rate_anomalies(paths, actions, critic_review=review)
    return actions


def record_critic_block_rate_anomalies(
    paths: BrainPaths,
    actions: list[dict[str, Any]],
    *,
    critic_review: dict[str, Any],
) -> None:
    threshold = critic_review.get("block_rate_anomaly_threshold")
    if threshold is None:
        return
    try:
        parsed_threshold = float(threshold)
    except (TypeError, ValueError):
        parsed_threshold = DEFAULT_CRITIC_BLOCK_RATE_ANOMALY_THRESHOLD
    min_reviewed = normalize_positive_int(
        critic_review.get("block_rate_anomaly_min_reviewed"),
        DEFAULT_CRITIC_BLOCK_RATE_ANOMALY_MIN_REVIEWED,
    )
    per_doc: dict[str, dict[str, Any]] = {}
    for action in actions:
        if action.get("action_type") != "fact_upsert":
            continue
        critic_decision = action.get("critic_decision")
        if critic_decision is None:
            continue
        fact = (action.get("evidence_json") or {}).get("payload", {}).get("fact", {})
        metadata = fact.get("metadata") if isinstance(fact, dict) else {}
        document_id = str((metadata or {}).get("document_id") or "").strip()
        if not document_id:
            continue
        bucket = per_doc.setdefault(
            document_id,
            {
                "reviewed": 0,
                "blocked": 0,
                "action_ids": [],
                "blocked_action_ids": [],
            },
        )
        bucket["reviewed"] += 1
        bucket["action_ids"].append(action["id"])
        if critic_decision != "agree":
            bucket["blocked"] += 1
            bucket["blocked_action_ids"].append(action["id"])
    with connection(paths.sqlite_path) as conn:
        for document_id, bucket in per_doc.items():
            reviewed = int(bucket["reviewed"])
            blocked = int(bucket["blocked"])
            if reviewed < min_reviewed:
                continue
            block_rate = blocked / reviewed if reviewed else 0.0
            if block_rate < parsed_threshold:
                continue
            existing = conn.execute(
                """
                SELECT 1
                FROM open_questions
                WHERE kind = 'document_extraction_anomaly'
                  AND status IN ('open', 'needs_human')
                  AND json_extract(context, '$.document_id') = ?
                LIMIT 1
                """,
                (document_id,),
            ).fetchone()
            if existing is not None:
                continue
            doc = conn.execute(
                "SELECT title, source_type FROM documents WHERE id = ?",
                (document_id,),
            ).fetchone()
            title = str(doc["title"] if doc else document_id)
            question_id = new_id("question")
            conn.execute(
                """
                INSERT INTO open_questions(
                  id, kind, entity_key, page_hint, fact_ids, question, options,
                  status, context, recommended_action, risk_tier, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    question_id,
                    "document_extraction_anomaly",
                    None,
                    None,
                    dumps([]),
                    (
                        f"Critic blocked {blocked}/{reviewed} extracted facts "
                        f"({block_rate:.0%}) for source document '{title}'. "
                        "Review extractor quality for this document before trusting its yield."
                    ),
                    dumps([]),
                    "needs_human",
                    dumps(
                        {
                            "document_id": document_id,
                            "title": title,
                            "reviewed_action_ids": bucket["action_ids"],
                            "blocked_action_ids": bucket["blocked_action_ids"],
                            "block_rate": block_rate,
                        }
                    ),
                    dumps({"action_type": "review_document_extraction"}),
                    "medium",
                    now_iso(),
                ),
            )


def earned_fact_decision(
    paths: BrainPaths, candidate: dict[str, Any]
) -> dict[str, Any]:
    page_hint = normalize_extraction_page_hint(str(candidate.get("page_hint") or ""))
    routing = candidate_route_metadata(candidate)
    if (
        page_hint in DEFAULT_FALLBACK_PAGE_HINTS
        and routing.get("route_review_reason") == "fallback_page"
    ):
        return simple_residue_decision(
            "unrouted_fact",
            "Extractor routed the candidate to the fallback page; review routing before applying.",
        )
    if routing.get("route_destination_valid") is False:
        return simple_residue_decision(
            "unrouted_fact",
            "Extractor routed the candidate outside canonical fact pages; review routing before applying.",
        )
    if page_hint in DEFAULT_FALLBACK_PAGE_HINTS:
        return simple_residue_decision(
            "unrouted_fact",
            "Extractor routed the candidate to the fallback page; review routing before applying.",
        )
    if not candidate.get("source_spans") or not candidate.get("evidence_quote"):
        return simple_residue_decision(
            "weak_evidence_fact",
            "Candidate does not have unit-derived source spans.",
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
    conflict = resolver_precheck_conflict(paths, candidate)
    resolver_precheck_evidence: dict[str, Any] | None = None
    if conflict:
        counterpart_facts = facts_for_ids(paths, conflict["counterpart_fact_ids"])
        judgment = resolver_precheck_conflict_judgment(
            paths, candidate, counterpart_facts
        )
        conflict["resolver_judgment"] = judgment
        if judgment["decision"] == "conflict":
            selected_ids = [
                fact_id
                for fact_id in judgment["counterpart_fact_ids"]
                if fact_id in conflict["counterpart_fact_ids"]
            ]
            conflict["counterpart_fact_ids"] = (
                selected_ids or conflict["counterpart_fact_ids"]
            )
            return simple_residue_decision(
                "fact_conflict_review",
                str(conflict["reason"]),
                target_fact_ids=conflict["counterpart_fact_ids"],
                evidence={"resolver_precheck": conflict},
            )
        resolver_precheck_evidence = {
            "resolver_precheck": {
                **conflict,
                "counterpart_fact_ids": [],
                "reason": "Resolver confirmed the candidate can coexist with nearby facts.",
            }
        }
    return {
        "decision": "apply",
        "reason": (
            "Unit-backed, routed candidate; resolver confirmed nearby facts can coexist."
            if resolver_precheck_evidence
            else "Unit-backed, routed candidate with no resolver precheck conflict signal."
        ),
        "risk_tier": "medium",
        "fact_upsert_resolution": "new_clean_fact",
        "target_fact_ids": [],
        **(
            {"evidence": resolver_precheck_evidence}
            if resolver_precheck_evidence
            else {}
        ),
    }


def earned_fact_action_features(
    candidate: dict[str, Any], decision: dict[str, Any]
) -> dict[str, Any]:
    page_hint = normalize_extraction_page_hint(str(candidate.get("page_hint") or ""))
    routing = candidate_route_metadata(candidate)
    quote_backed = bool(
        candidate.get("source_spans") and candidate.get("evidence_quote")
    )
    clean = (
        decision.get("decision") == "apply"
        and quote_backed
        and page_hint not in DEFAULT_FALLBACK_PAGE_HINTS
        and routing.get("route_destination_valid") is not False
        and decision.get("fact_upsert_resolution")
        in {"new_clean_fact", "exact_duplicate_source_union"}
    )
    features = {
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
        "route_destination_valid": routing.get("route_destination_valid"),
        "route_target_exists": routing.get("route_target_exists"),
        "route_resolution": routing.get("route_resolution"),
        "original_page_hint": routing.get("original_page_hint"),
        "resolver_precheck": "passed" if clean else "residue",
        "confidence": candidate.get("truth_confidence"),
        "extraction_confidence": candidate.get("extraction_confidence"),
        "routing_confidence": candidate.get("routing_confidence"),
        "truth_confidence": candidate.get("truth_confidence"),
        "eval_gate": {"suite": "extraction", "requires_labels": True},
    }
    if decision.get("residue_kind") == "fact_conflict_review":
        features["resolver_precheck_counterpart_fact_ids"] = (
            decision.get("target_fact_ids") or []
        )
    return features


RECLAIM_ROUTE_EXTRA_STOP_TOKENS = {
    "agent",
    "agents",
    "analytics",
    "business",
    "customer",
    "customers",
    "data",
    "delivery",
    "enterprise",
    "market",
    "management",
    "model",
    "models",
    "native",
    "platform",
    "product",
    "products",
    "proposition",
    "role",
    "service",
    "services",
    "strategy",
    "value",
    "vision",
    "workflow",
    "workflows",
}

DOCUMENT_ROUTE_UNCERTAIN_CONFIDENCE = 0.65
DOCUMENT_ROUTE_MIN_SIBLINGS = 2
DOCUMENT_ROUTE_MIN_SHARE = 0.6


def apply_document_route_coherence(
    candidates: list[dict[str, Any]],
    route_targets: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    priors = [
        prior
        for prior in route_priors_from_facts(candidates)
        if str(prior["page_hint"]) in route_targets
    ]
    if not priors:
        return candidates
    output: list[dict[str, Any]] = []
    for candidate in candidates:
        routing = candidate_route_metadata(candidate)
        confidence = optional_float(candidate.get("routing_confidence"))
        current_page_hint = normalize_extraction_page_hint(
            str(candidate.get("page_hint") or "")
        )
        uncertain = (
            routing.get("route_destination_valid") is False
            or current_page_hint in DEFAULT_FALLBACK_PAGE_HINTS
            or (
                confidence is not None
                and confidence < DOCUMENT_ROUTE_UNCERTAIN_CONFIDENCE
            )
        )
        if not uncertain:
            output.append(candidate)
            continue
        choices: list[tuple[float, str, dict[str, Any], dict[str, Any]]] = []
        query_text = fact_route_reclaim_query(candidate)
        for prior in priors:
            if (
                int(prior.get("fact_count") or 0) < DOCUMENT_ROUTE_MIN_SIBLINGS
                or float(prior.get("share") or 0.0) < DOCUMENT_ROUTE_MIN_SHARE
            ):
                continue
            hint = route_targets[str(prior["page_hint"])]
            lexical = score_reclaim_route(query_text, hint)
            has_fact_support = bool(
                lexical["overlap"] or float(lexical["phrase_bonus"] or 0.0) > 0
            )
            if not has_fact_support and not strong_document_prior(prior):
                continue
            total_score = float(lexical["score"]) + coherence_bonus(prior)
            choices.append((total_score, str(prior["page_hint"]), prior, lexical))
        if not choices:
            output.append(candidate)
            continue
        choices.sort(key=lambda choice: (choice[0], choice[1]), reverse=True)
        total_score, selected_page_hint, prior, lexical = choices[0]
        if selected_page_hint == current_page_hint:
            output.append(candidate)
            continue
        page_hint, resolved_routing = resolve_extraction_page_hint(
            selected_page_hint, route_targets
        )
        if (
            resolved_routing.get("route_destination_valid") is False
            or page_hint in DEFAULT_FALLBACK_PAGE_HINTS
        ):
            output.append(candidate)
            continue
        routed = dict(candidate)
        routed["page_hint"] = page_hint
        routed["entity_key"] = entity_key_for_change(
            topic_for_path(page_hint),
            page_hint,
            str(routed.get("section_hint") or "Summary"),
        )
        metadata = (
            dict(routed.get("metadata") or {})
            if isinstance(routed.get("metadata"), dict)
            else {}
        )
        metadata["routing"] = {
            **resolved_routing,
            "route_resolution": "document_coherence_reroute",
            "coherence_original_page_hint": current_page_hint,
            "document_coherence_fact_count": prior["fact_count"],
            "document_coherence_share": prior["share"],
            "document_coherence_bonus": coherence_bonus(prior),
            "document_coherence_lexical_overlap": lexical["overlap"],
            "document_coherence_score": round(total_score, 4),
        }
        routed["metadata"] = metadata
        output.append(routed)
    return output


def reclaim_route_tokens(value: str) -> set[str]:
    return routing_signal_tokens(value) - RECLAIM_ROUTE_EXTRA_STOP_TOKENS


def score_reclaim_route(query_text: str, hint: dict[str, Any]) -> dict[str, Any]:
    query_tokens = reclaim_route_tokens(query_text)
    hint_tokens = reclaim_route_tokens(routing_hint_text(hint))
    overlap = sorted(query_tokens & hint_tokens)
    phrase_bonus = routing_phrase_bonus(query_text, hint)
    source_bonus = 1.0 if hint.get("_routing_source") == "page_contract" else 0.0
    score = source_bonus + phrase_bonus
    if overlap:
        score += float(len(overlap) * 5)
        score += float(len(overlap)) / max(1.0, float(len(hint_tokens)))
    return {
        "page_hint": str(hint.get("page_hint") or ""),
        "score": round(score, 4),
        "overlap": overlap,
        "phrase_bonus": round(phrase_bonus, 4),
        "source": hint.get("_routing_source"),
    }


def select_reclaim_route(
    candidate: dict[str, Any],
    route_targets: dict[str, dict[str, Any]],
    *,
    min_score: float,
    min_overlap: int,
    document_priors: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    original_page_hint = normalize_extraction_page_hint(
        str(candidate.get("page_hint") or "")
    )
    if original_page_hint and original_page_hint not in DEFAULT_FALLBACK_PAGE_HINTS:
        resolved_page_hint, routing = resolve_extraction_page_hint(
            original_page_hint, route_targets
        )
        if (
            routing.get("route_destination_valid") is not False
            and resolved_page_hint not in DEFAULT_FALLBACK_PAGE_HINTS
            and routing.get("route_resolution") != "new_canonical_page"
        ):
            return {
                "page_hint": resolved_page_hint,
                "score": None,
                "overlap": [],
                "phrase_bonus": None,
                "source": "original_page_hint",
            }

    query_text = fact_route_reclaim_query(candidate)
    priors_by_page = {
        str(prior.get("page_hint") or ""): prior
        for prior in document_priors or []
        if prior.get("page_hint")
    }
    best: dict[str, Any] | None = None
    for hint in route_targets.values():
        page_hint = normalize_extraction_page_hint(str(hint.get("page_hint") or ""))
        if not page_hint or page_hint in DEFAULT_FALLBACK_PAGE_HINTS:
            continue
        scored = score_reclaim_route(query_text, hint)
        prior = priors_by_page.get(page_hint) or {}
        prior_bonus = coherence_bonus(prior)
        scored["score"] = round(float(scored["score"]) + prior_bonus, 4)
        scored["document_coherence_fact_count"] = int(prior.get("fact_count") or 0)
        scored["document_coherence_share"] = float(prior.get("share") or 0.0)
        scored["document_coherence_bonus"] = prior_bonus
        overlap_count = len(scored["overlap"])
        coherence_supported = (
            int(prior.get("fact_count") or 0) >= DOCUMENT_ROUTE_MIN_SIBLINGS
            and float(prior.get("share") or 0.0) >= DOCUMENT_ROUTE_MIN_SHARE
            and overlap_count >= 1
        )
        if overlap_count < min_overlap:
            if (
                float(scored["phrase_bonus"] or 0.0) <= 0.0
                and not coherence_supported
                and not strong_document_prior(prior)
            ):
                continue
            namespace = page_hint.split("/", 1)[0]
            if (
                namespace not in {"companies", "people"}
                and not coherence_supported
                and not strong_document_prior(prior)
            ):
                continue
        if float(scored["score"]) < min_score and not (
            strong_document_prior(prior) and float(scored["score"]) >= 4.0
        ):
            continue
        if best is None or (float(scored["score"]), page_hint) > (
            float(best["score"]),
            str(best["page_hint"]),
        ):
            best = scored
    return best


def reroute_unrouted_candidate(
    candidate: dict[str, Any],
    route_targets: dict[str, dict[str, Any]],
    *,
    min_score: float,
    min_overlap: int,
    document_priors: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    selected = select_reclaim_route(
        candidate,
        route_targets,
        min_score=min_score,
        min_overlap=min_overlap,
        document_priors=document_priors,
    )
    if selected is None:
        return None
    routed = json.loads(json.dumps(candidate))
    original_page_hint = normalize_extraction_page_hint(
        str(candidate.get("page_hint") or "")
    )
    page_hint, routing = resolve_extraction_page_hint(
        str(selected["page_hint"]), route_targets
    )
    if (
        routing.get("route_destination_valid") is False
        or page_hint in DEFAULT_FALLBACK_PAGE_HINTS
    ):
        return None
    routed["page_hint"] = page_hint
    section_hint = str(routed.get("section_hint") or "")
    routed["entity_key"] = entity_key_for_change(
        topic_for_path(page_hint), page_hint, section_hint
    )
    metadata = (
        routed.get("metadata") if isinstance(routed.get("metadata"), dict) else {}
    )
    metadata = dict(metadata)
    metadata["routing"] = {
        **routing,
        "reclaimed_from_page_hint": original_page_hint,
        "reclaim_route_score": selected.get("score"),
        "reclaim_route_overlap": selected.get("overlap") or [],
        "reclaim_route_source": selected.get("source"),
        "document_coherence_fact_count": selected.get(
            "document_coherence_fact_count", 0
        ),
        "document_coherence_share": selected.get("document_coherence_share", 0.0),
        "document_coherence_bonus": selected.get("document_coherence_bonus", 0.0),
    }
    routed["metadata"] = metadata
    return routed


def reclaim_unrouted_facts(
    paths: BrainPaths,
    *,
    dry_run: bool = True,
    limit: int | None = None,
    min_score: float = 8.0,
    min_overlap: int = 2,
    critic_review: dict[str, Any] | None = None,
) -> dict[str, Any]:
    route_targets = load_extraction_route_targets(paths)
    limit_clause = "LIMIT ?" if limit is not None else ""
    params: tuple[Any, ...] = (limit,) if limit is not None else ()
    with connection(paths.sqlite_path) as conn:
        question_rows = rows(
            conn,
            f"""
            SELECT *
            FROM open_questions
            WHERE kind = 'unrouted_fact'
              AND status IN ('open', 'needs_human')
            ORDER BY created_at, id
            {limit_clause}
            """,
            params,
        )
    reclaimable: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    unresolved: list[tuple[Any, dict[str, Any], dict[str, Any]]] = []
    document_prior_cache: dict[str, list[dict[str, Any]]] = {}
    for question in question_rows:
        action_id = str(question["action_id"] or "")
        if not action_id:
            skipped.append(
                {"question_id": question["id"], "reason": "missing_action_id"}
            )
            continue
        try:
            action = get_action(paths, action_id)
        except ValueError:
            skipped.append(
                {
                    "question_id": question["id"],
                    "action_id": action_id,
                    "reason": "missing_action",
                }
            )
            continue
        payload = (action.get("evidence_json") or {}).get("payload") or {}
        candidate = payload.get("fact") if isinstance(payload, dict) else None
        if not isinstance(candidate, dict):
            skipped.append(
                {
                    "question_id": question["id"],
                    "action_id": action_id,
                    "reason": "missing_fact_payload",
                }
            )
            continue
        document_id = fact_document_id(candidate)
        if document_id not in document_prior_cache:
            with connection(paths.sqlite_path) as conn:
                document_prior_cache[document_id] = load_document_route_priors(
                    conn, document_id
                )
        rerouted = reroute_unrouted_candidate(
            candidate,
            route_targets,
            min_score=min_score,
            min_overlap=min_overlap,
            document_priors=document_prior_cache[document_id],
        )
        if rerouted is None:
            unresolved.append((question, action, candidate))
            continue
        reclaimable.append(reclaim_route_record(question, action, candidate, rerouted))
    llm_routed = resolve_unrouted_candidate_routes(
        paths, [item[2] for item in unresolved], route_targets
    )
    for (question, action, candidate), rerouted in zip(unresolved, llm_routed):
        if candidate_requires_route_resolution(rerouted):
            skipped.append(
                {
                    "question_id": question["id"],
                    "action_id": question["action_id"],
                    "reason": "resolver_requires_human",
                    "statement": str(candidate.get("statement") or "")[:180],
                }
            )
            continue
        reclaimable.append(reclaim_route_record(question, action, candidate, rerouted))
    preview = [
        {
            key: value
            for key, value in item.items()
            if key not in {"candidate", "old_evidence_json"}
        }
        for item in reclaimable
    ]
    if dry_run or not reclaimable:
        return {
            "status": "dry_run" if dry_run else "ok",
            "inspected": len(question_rows),
            "reclaimable": len(reclaimable),
            "skipped": len(skipped),
            "preview": preview[:25],
            "skipped_examples": skipped[:25],
        }

    review = critic_review or {
        **default_critic_review_config(),
        "disagreement_mode": "reject",
    }
    actions = propose_policy_gated_candidates(
        paths,
        [item["candidate"] for item in reclaimable],
        run_id=None,
        critic_review=review,
    )
    timestamp = now_iso()
    resolved = 0
    with connection(paths.sqlite_path) as conn:
        for item, new_action in zip(reclaimable, actions):
            answer = {
                "reason": "unrouted fact reclaimed against full route pool",
                "old_action_id": item["old_action_id"],
                "new_action_id": new_action["id"],
                "new_action_status": new_action["status"],
                "new_page_hint": item["new_page_hint"],
                "route_score": item["route_score"],
                "route_overlap": item["route_overlap"],
            }
            conn.execute(
                """
                UPDATE open_questions
                SET status = 'auto_resolved',
                    answer = ?,
                    answered_at = ?,
                    decided_by = ?
                WHERE id = ?
                """,
                (
                    dumps(answer),
                    timestamp,
                    "reclaim_unrouted_facts",
                    item["question_id"],
                ),
            )
            old_row = conn.execute(
                "SELECT * FROM cos_actions WHERE id = ?", (item["old_action_id"],)
            ).fetchone()
            if old_row is not None:
                evidence = dict(item.get("old_evidence_json") or {})
                evidence["reclaimed_by_action_id"] = new_action["id"]
                evidence["reclaim_answer"] = answer
                conn.execute(
                    """
                    UPDATE cos_actions
                    SET status = 'rejected',
                        policy_decision = COALESCE(policy_decision, 'reclaimed_unrouted'),
                        evidence_json = ?
                    WHERE id = ?
                    """,
                    (dumps(evidence), item["old_action_id"]),
                )
            resolved += 1
    return {
        "status": "ok",
        "inspected": len(question_rows),
        "reclaimable": len(reclaimable),
        "resolved": resolved,
        "skipped": len(skipped),
        "actions": [
            {
                "id": action["id"],
                "status": action["status"],
                "critic_decision": action.get("critic_decision"),
                "audit_status": action.get("audit_status"),
            }
            for action in actions
        ],
        "preview": preview[:25],
        "skipped_examples": skipped[:25],
    }


def resolver_precheck_conflict_reason(
    paths: BrainPaths, candidate: dict[str, Any]
) -> str | None:
    conflict = resolver_precheck_conflict(paths, candidate)
    return str(conflict["reason"]) if conflict else None


def resolver_precheck_conflict(
    paths: BrainPaths, candidate: dict[str, Any]
) -> dict[str, Any] | None:
    statement = str(candidate.get("statement") or "")
    entity_id = str(candidate.get("entity_id") or "").strip()
    mention = str(
        (candidate.get("metadata") or {}).get("model_entity_key")
        or candidate.get("entity_mention")
        or ""
    ).strip()
    with connection(paths.sqlite_path) as conn:
        if not entity_id and mention:
            resolution = resolve_entity(
                conn,
                mention,
                type_hint=candidate.get("entity_type"),
                create=False,
            )
            entity_id = resolution.entity_id if resolution else ""
        if entity_id:
            fact_rows = rows(
                conn,
                """
                SELECT *
                FROM facts
                WHERE entity_id = ?
                  AND status IN ('active', 'conflicted')
                ORDER BY observed_at DESC, created_at DESC
                LIMIT 50
                """,
                (entity_id,),
            )
        else:
            fact_rows = []
        page_hint = normalize_extraction_page_hint(
            str(candidate.get("page_hint") or "")
        )
        if page_hint:
            page_fact_rows = rows(
                conn,
                """
                SELECT *
                FROM facts
                WHERE page_hint = ?
                  AND status IN ('active', 'conflicted')
                ORDER BY observed_at DESC, created_at DESC
                LIMIT 50
                """,
                (page_hint,),
            )
            seen_fact_ids = {str(row["id"]) for row in fact_rows}
            fact_rows.extend(
                row for row in page_fact_rows if str(row["id"]) not in seen_fact_ids
            )
        if not fact_rows:
            return None
    directly_conflicting_fact_ids: list[str] = []
    relation_classifications: list[dict[str, Any]] = []
    for row in fact_rows:
        fact = row_to_fact(row)
        if facts_directly_conflict(fact, {"statement": statement}):
            directly_conflicting_fact_ids.append(str(fact["id"]))
    if directly_conflicting_fact_ids:
        counterpart_facts = [
            fact
            for fact in (row_to_fact(row) for row in fact_rows)
            if str(fact["id"]) in directly_conflicting_fact_ids
        ]
        for fact in counterpart_facts[:5]:
            relation = classify_fact_relation(candidate, fact).as_dict()
            relation_classifications.append(relation)
        contradictions = [
            item
            for item in relation_classifications
            if item["relation"] == "contradicts"
            and float(item.get("confidence") or 0.0) >= 0.7
        ]
        if not contradictions:
            return None
        selected_fact_ids = [
            str(item["existing_fact_id"])
            for item in contradictions
            if str(item.get("existing_fact_id") or "") in directly_conflicting_fact_ids
        ]
        return {
            "reason": (
                "Pairwise relation classifier says the candidate may not coexist "
                "with existing fact(s) in the same scope."
            ),
            "counterpart_fact_ids": selected_fact_ids
            or directly_conflicting_fact_ids[:5],
            "entity_id": entity_id,
            "entity_mention": mention,
            "precheck": "relation_classifier",
            "relation_classifications": relation_classifications,
        }
    return None


def facts_for_ids(paths: BrainPaths, fact_ids: list[str]) -> list[dict[str, Any]]:
    requested = [str(fact_id) for fact_id in fact_ids if str(fact_id).strip()]
    if not requested:
        return []
    placeholders = ",".join("?" for _ in requested)
    with connection(paths.sqlite_path) as conn:
        by_id = {
            str(row["id"]): row_to_fact(row)
            for row in conn.execute(
                f"SELECT * FROM facts WHERE id IN ({placeholders})",
                requested,
            )
        }
    return [by_id[fact_id] for fact_id in requested if fact_id in by_id]


def resolver_precheck_conflict_judgment(
    paths: BrainPaths,
    candidate: dict[str, Any],
    counterpart_facts: list[dict[str, Any]],
) -> dict[str, Any]:
    if not counterpart_facts:
        return {
            "decision": "no_conflict",
            "counterpart_fact_ids": [],
            "rationale": "No counterpart facts were supplied.",
        }
    counterpart_fact_ids = [str(fact["id"]) for fact in counterpart_facts]
    if not cos_role_provider_configured(paths, "resolver"):
        return {
            "decision": "conflict",
            "counterpart_fact_ids": counterpart_fact_ids,
            "rationale": "Resolver role is not configured; failing closed to review.",
        }
    try:
        parsed = complete_json(
            conflict_precheck_prompt(candidate, counterpart_facts),
            schema=CONFLICT_PRECHECK_SCHEMA,
            role="resolver",
            paths=paths,
        )
    except Exception as exc:
        return {
            "decision": "conflict",
            "counterpart_fact_ids": counterpart_fact_ids,
            "rationale": f"Resolver precheck failed; failing closed to review: {exc}",
        }
    decision = str(parsed.get("decision") or "").strip().lower()
    if decision not in {"conflict", "no_conflict"}:
        decision = "conflict"
    selected_ids = [
        str(fact_id)
        for fact_id in parsed.get("counterpart_fact_ids") or []
        if str(fact_id) in counterpart_fact_ids
    ]
    return {
        "decision": decision,
        "counterpart_fact_ids": (
            (selected_ids or counterpart_fact_ids) if decision == "conflict" else []
        ),
        "rationale": str(parsed.get("rationale") or "")[:1000],
    }


def conflict_precheck_prompt(
    candidate: dict[str, Any], counterpart_facts: list[dict[str, Any]]
) -> str:
    candidate_card = {
        "statement": candidate.get("statement"),
        "entity_key": candidate.get("entity_key"),
        "entity_id": candidate.get("entity_id"),
        "page_hint": candidate.get("page_hint"),
        "section_hint": candidate.get("section_hint"),
        "evidence_quote": candidate.get("evidence_quote"),
        "source_ids": candidate.get("source_ids") or [],
    }
    counterpart_cards = [
        {
            "id": fact.get("id"),
            "statement": fact.get("statement"),
            "entity_key": fact.get("entity_key"),
            "entity_id": fact.get("entity_id"),
            "page_hint": fact.get("page_hint"),
            "section_hint": fact.get("section_hint"),
            "evidence_quote": fact.get("evidence_quote"),
            "source_ids": fact.get("source_ids") or [],
            "status": fact.get("status"),
        }
        for fact in counterpart_facts
    ]
    return (
        "Judge whether a proposed PKM fact genuinely contradicts existing facts. "
        "Return decision 'conflict' only when the candidate and at least one existing fact "
        "cannot both be true under the same entity, topic, time, and scope. Return "
        "'no_conflict' when the facts could coexist, including when context is insufficient "
        "to prove a direct contradiction. Return 'no_conflict' for unrelated facts, "
        "complementary facts, different attributes of the same entity, same-topic facts that "
        "can both be true, or lexical cue matches caused only by words like before/after, "
        "not, rather than, high/low, or different numbers. Do not judge whether the candidate "
        "is source-supported; the critic handles evidence support separately. If conflict, "
        "include the existing counterpart_fact_ids that conflict.\n\n"
        f"Candidate:\n{candidate_card}\n\nExisting facts:\n{counterpart_cards}"
    )


def backfill_fact_conflict_review_questions(paths: BrainPaths) -> dict[str, Any]:
    """Repair pre-fix extraction conflict questions so reviewers can see both sides."""
    with connection(paths.sqlite_path) as conn:
        questions = [
            dict(row)
            for row in conn.execute(
                """
                SELECT *
                FROM open_questions
                WHERE kind = 'fact_conflict_review'
                  AND status IN ('open', 'needs_human')
                ORDER BY created_at
                """
            )
        ]
    inspected = 0
    updated = 0
    skipped: list[dict[str, str]] = []
    for question in questions:
        inspected += 1
        action_id = str(question.get("action_id") or "")
        if not action_id:
            skipped.append(
                {"question_id": question["id"], "reason": "missing_action_id"}
            )
            continue
        try:
            action = get_action(paths, action_id)
        except ValueError:
            skipped.append({"question_id": question["id"], "reason": "missing_action"})
            continue
        evidence = dict(action.get("evidence_json") or {})
        candidate = (evidence.get("payload") or {}).get("fact") or {}
        if not isinstance(candidate, dict) or not candidate:
            skipped.append(
                {"question_id": question["id"], "reason": "missing_candidate"}
            )
            continue
        conflict = resolver_precheck_conflict(paths, candidate)
        if not conflict:
            skipped.append(
                {"question_id": question["id"], "reason": "no_counterpart_found"}
            )
            continue
        counterpart_fact_ids = [str(item) for item in conflict["counterpart_fact_ids"]]
        evidence["resolver_precheck"] = conflict
        features = dict(action.get("action_features") or {})
        features["resolver_precheck_counterpart_fact_ids"] = counterpart_fact_ids
        with connection(paths.sqlite_path) as conn:
            conn.execute(
                """
                UPDATE cos_actions
                SET target_fact_ids = ?, evidence_json = ?, action_features = ?
                WHERE id = ?
                """,
                (
                    dumps(counterpart_fact_ids),
                    dumps(evidence),
                    dumps(features),
                    action_id,
                ),
            )
        refreshed = get_action(paths, action_id)
        with connection(paths.sqlite_path) as conn:
            refresh_action_residue_question(
                conn,
                refreshed,
                str(question["id"]),
                kind="fact_conflict_review",
                reason=str(conflict["reason"]),
            )
        updated += 1
    return {
        "status": "ok",
        "inspected": inspected,
        "updated": updated,
        "skipped": skipped,
    }


def reconcile_fact_conflict_reviews(
    paths: BrainPaths,
    *,
    dry_run: bool = True,
    limit: int | None = None,
    critic_review: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Re-run active extraction conflicts through the strict pairwise gate."""
    query = """
        SELECT id, action_id, created_at
        FROM open_questions
        WHERE kind = 'fact_conflict_review'
          AND status IN ('open', 'needs_human')
        ORDER BY created_at, id
    """
    params: list[Any] = []
    if limit is not None:
        query += " LIMIT ?"
        params.append(max(1, int(limit)))
    with connection(paths.sqlite_path) as conn:
        question_rows = [dict(row) for row in conn.execute(query, params)]

    released: list[dict[str, Any]] = []
    retained: list[dict[str, Any]] = []
    terminal: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    preview: list[dict[str, Any]] = []
    terminal_statuses = {
        "auto_applied",
        "applied",
        "rejected",
        "dismissed",
        "failed",
        "reverted",
    }
    for question in question_rows:
        question_id = str(question["id"])
        action_id = str(question.get("action_id") or "")
        if not action_id:
            skipped.append({"question_id": question_id, "reason": "missing_action_id"})
            continue
        try:
            action = get_action(paths, action_id)
        except ValueError:
            skipped.append({"question_id": question_id, "reason": "missing_action"})
            continue
        if str(action.get("status") or "") in terminal_statuses:
            terminal.append(
                {
                    "question_id": question_id,
                    "action_id": action_id,
                    "action_status": action["status"],
                }
            )
            continue
        evidence = dict(action.get("evidence_json") or {})
        candidate = (evidence.get("payload") or {}).get("fact") or {}
        if not isinstance(candidate, dict) or not candidate:
            skipped.append(
                {
                    "question_id": question_id,
                    "action_id": action_id,
                    "reason": "missing_candidate_payload",
                }
            )
            continue
        conflict = resolver_precheck_conflict(paths, candidate)
        item = {
            "question_id": question_id,
            "action_id": action_id,
            "statement": str(candidate.get("statement") or "")[:240],
            "action": action,
            "candidate": candidate,
            "conflict": conflict,
        }
        if conflict is None:
            item["reason"] = "no_direct_pairwise_contradiction"
            released.append(item)
            preview.append(
                {
                    "question_id": question_id,
                    "action_id": action_id,
                    "outcome": "release",
                    "reason": item["reason"],
                    "statement": item["statement"],
                }
            )
            continue
        if dry_run:
            item["reason"] = "resolver_confirmation_required"
            retained.append(item)
            preview.append(
                {
                    "question_id": question_id,
                    "action_id": action_id,
                    "outcome": "resolver_review",
                    "counterpart_fact_ids": conflict["counterpart_fact_ids"],
                    "statement": item["statement"],
                }
            )
            continue
        counterpart_facts = facts_for_ids(paths, conflict["counterpart_fact_ids"])
        judgment = resolver_precheck_conflict_judgment(
            paths, candidate, counterpart_facts
        )
        conflict["resolver_judgment"] = judgment
        if judgment["decision"] == "no_conflict":
            conflict["counterpart_fact_ids"] = []
            item["reason"] = "resolver_confirmed_coexistence"
            released.append(item)
            preview.append(
                {
                    "question_id": question_id,
                    "action_id": action_id,
                    "outcome": "release",
                    "reason": item["reason"],
                    "statement": item["statement"],
                }
            )
            continue
        selected_ids = [
            fact_id
            for fact_id in judgment["counterpart_fact_ids"]
            if fact_id in conflict["counterpart_fact_ids"]
        ]
        conflict["counterpart_fact_ids"] = (
            selected_ids or conflict["counterpart_fact_ids"]
        )
        conflict["reason"] = (
            "A direct pairwise contradiction remains after resolver review."
        )
        item["reason"] = "direct_conflict_retained"
        retained.append(item)
        preview.append(
            {
                "question_id": question_id,
                "action_id": action_id,
                "outcome": "retain",
                "counterpart_fact_ids": conflict["counterpart_fact_ids"],
                "statement": item["statement"],
            }
        )

    if dry_run:
        return {
            "status": "dry_run",
            "inspected": len(question_rows),
            "release_without_resolver": len(released),
            "resolver_review_required": len(retained),
            "terminal_question_count": len(terminal),
            "skipped_count": len(skipped),
            "preview": preview[:25],
            "skipped": skipped[:25],
        }

    timestamp = now_iso()
    for item in retained:
        action = item["action"]
        conflict = item["conflict"]
        evidence = dict(action.get("evidence_json") or {})
        evidence["resolver_precheck"] = conflict
        evidence["conflict_reconciliation"] = {
            "version": "pairwise-v2",
            "outcome": "retained",
            "reason": item["reason"],
            "question_id": item["question_id"],
            "reconciled_at": timestamp,
        }
        features = dict(action.get("action_features") or {})
        features.update(
            {
                "clean_fact_upsert": False,
                "fact_upsert_resolution": None,
                "simple_decision": "residue",
                "residue_kind": "fact_conflict_review",
                "resolver_precheck": "residue",
                "resolver_precheck_counterpart_fact_ids": conflict[
                    "counterpart_fact_ids"
                ],
                "risk_tier": "high",
            }
        )
        with connection(paths.sqlite_path) as conn:
            conn.execute(
                """
                UPDATE cos_actions
                SET target_fact_ids = ?, action_features = ?, evidence_json = ?,
                    risk_tier = 'high'
                WHERE id = ?
                """,
                (
                    dumps(conflict["counterpart_fact_ids"]),
                    dumps(features),
                    dumps(evidence),
                    item["action_id"],
                ),
            )
        refreshed = get_action(paths, item["action_id"])
        with connection(paths.sqlite_path) as conn:
            refresh_action_residue_question(
                conn,
                refreshed,
                item["question_id"],
                kind="fact_conflict_review",
                reason=str(conflict["reason"]),
            )

    for item in released:
        action = item["action"]
        candidate = item["candidate"]
        evidence = dict(action.get("evidence_json") or {})
        conflict = item.get("conflict") or {}
        evidence["resolver_precheck"] = {
            **conflict,
            "counterpart_fact_ids": [],
            "precheck": "pairwise_v2_passed",
            "reason": (
                "No direct, same-scope contradiction remains after pairwise reconciliation."
            ),
        }
        evidence["conflict_reconciliation"] = {
            "version": "pairwise-v2",
            "outcome": "released",
            "reason": item["reason"],
            "question_id": item["question_id"],
            "reconciled_at": timestamp,
        }
        decision = {
            "decision": "apply",
            "fact_upsert_resolution": "new_clean_fact",
            "target_fact_ids": [],
        }
        features = dict(action.get("action_features") or {})
        for stale_key in (
            "classifier_version",
            "relation",
            "relation_confidence",
            "relation_rationale",
            "resolver_precheck_counterpart_fact_ids",
        ):
            features.pop(stale_key, None)
        features.update(earned_fact_action_features(candidate, decision))
        features["risk_tier"] = "medium"
        features["target_fact_ids"] = []
        with connection(paths.sqlite_path) as conn:
            conn.execute(
                """
                UPDATE cos_actions
                SET status = 'proposed', target_fact_ids = '[]',
                    action_features = ?, evidence_json = ?, risk_tier = 'medium',
                    policy_id = NULL, policy_version = NULL,
                    policy_decision = NULL, autonomy_level = NULL,
                    critic_by = NULL, critic_decision = NULL
                WHERE id = ?
                """,
                (dumps(features), dumps(evidence), item["action_id"]),
            )

    decided_actions = decide_policy_actions(
        paths,
        [item["action_id"] for item in released],
        critic_review=critic_review or default_critic_review_config(),
    )
    decided_by_id = {str(action["id"]): action for action in decided_actions}
    with connection(paths.sqlite_path) as conn:
        for item in released:
            result = decided_by_id.get(item["action_id"], {})
            conn.execute(
                """
                UPDATE open_questions
                SET status = 'auto_resolved', answer = ?, answered_at = ?,
                    decided_by = 'conflict_reconciliation_v2'
                WHERE id = ?
                """,
                (
                    dumps(
                        {
                            "decision": "released_to_policy",
                            "reason": item["reason"],
                            "action_id": item["action_id"],
                            "action_status": result.get("status"),
                            "policy_decision": result.get("policy_decision"),
                            "critic_decision": result.get("critic_decision"),
                        }
                    ),
                    timestamp,
                    item["question_id"],
                ),
            )
        for item in terminal:
            conn.execute(
                """
                UPDATE open_questions
                SET status = 'auto_resolved', answer = ?, answered_at = ?,
                    decided_by = 'conflict_reconciliation_v2'
                WHERE id = ?
                """,
                (
                    dumps(
                        {
                            "decision": "stale_question_closed",
                            "action_id": item["action_id"],
                            "action_status": item["action_status"],
                        }
                    ),
                    timestamp,
                    item["question_id"],
                ),
            )
    status_counts: dict[str, int] = {}
    for action in decided_actions:
        status = str(action.get("status") or "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1
    return {
        "status": "applied",
        "inspected": len(question_rows),
        "released": len(released),
        "retained": len(retained),
        "terminal_questions_closed": len(terminal),
        "skipped_count": len(skipped),
        "released_action_status_counts": status_counts,
        "preview": preview[:25],
        "skipped": skipped[:25],
    }


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
        action_fact = (
            decision.get("fact")
            if isinstance(decision.get("fact"), dict)
            else candidate
        )
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
    page_hint = normalize_extraction_page_hint(str(candidate.get("page_hint") or ""))
    routing = candidate_route_metadata(candidate)
    if (
        page_hint
        in set(
            simple_autonomy.get("fallback_page_hints") or DEFAULT_FALLBACK_PAGE_HINTS
        )
        and routing.get("route_review_reason") == "fallback_page"
    ):
        return simple_residue_decision(
            "unrouted_fact",
            "Extractor routed the candidate to the fallback page; review routing before applying.",
        )
    if routing.get("route_destination_valid") is False:
        return simple_residue_decision(
            "unrouted_fact",
            "Extractor routed the candidate outside canonical fact pages; review routing before applying.",
        )
    if page_hint in set(
        simple_autonomy.get("fallback_page_hints") or DEFAULT_FALLBACK_PAGE_HINTS
    ):
        return simple_residue_decision(
            "unrouted_fact",
            "Extractor routed the candidate to the fallback page; review routing before applying.",
        )
    if not candidate.get("source_spans") or not candidate.get("evidence_quote"):
        return simple_residue_decision(
            "weak_evidence_fact",
            "Candidate does not have unit-derived source spans.",
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
        "reason": "Unit-backed, routed candidate with no deterministic residue signal.",
        "risk_tier": "medium",
        "simple_resolution": "new_fact",
        "target_fact_ids": [],
    }


def simple_residue_decision(
    kind: str,
    reason: str,
    *,
    target_fact_ids: list[str] | None = None,
    evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "decision": "residue",
        "residue_kind": kind,
        "reason": reason,
        "risk_tier": "high",
        "target_fact_ids": target_fact_ids or [],
        **({"evidence": evidence} if evidence else {}),
    }


def simple_low_confidence_reason(
    candidate: dict[str, Any], simple_autonomy: dict[str, Any]
) -> str | None:
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


def simple_fact_action_features(
    candidate: dict[str, Any], decision: dict[str, Any]
) -> dict[str, Any]:
    page_hint = normalize_extraction_page_hint(str(candidate.get("page_hint") or ""))
    routing = candidate_route_metadata(candidate)
    return {
        "candidate_signal": "source_extraction",
        "simple_autonomy": True,
        "simple_decision": decision.get("decision"),
        "simple_resolution": decision.get("simple_resolution"),
        "residue_kind": decision.get("residue_kind"),
        "affected_fact_count": 1,
        "reversible": True,
        "truth_mutation": False,
        "quote_backed": bool(
            candidate.get("source_spans") and candidate.get("evidence_quote")
        ),
        "fallback_route": page_hint in DEFAULT_FALLBACK_PAGE_HINTS,
        "route_destination_valid": routing.get("route_destination_valid"),
        "route_target_exists": routing.get("route_target_exists"),
        "route_resolution": routing.get("route_resolution"),
        "original_page_hint": routing.get("original_page_hint"),
    }


def find_exact_duplicate_fact(
    paths: BrainPaths, candidate: dict[str, Any]
) -> dict[str, Any] | None:
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
    candidate_metadata = (
        candidate.get("metadata") if isinstance(candidate.get("metadata"), dict) else {}
    )
    for key in ("document_id", "window_id", "evidence_units"):
        if candidate_metadata.get(key) is not None:
            metadata[key] = candidate_metadata[key]
    metadata.setdefault("simple_autonomy", {})
    metadata["simple_autonomy"] = {
        **(
            metadata.get("simple_autonomy")
            if isinstance(metadata.get("simple_autonomy"), dict)
            else {}
        ),
        "last_resolution": "exact_duplicate_source_union",
        "last_extractor_model": candidate.get("extractor_model"),
    }
    return {
        **existing,
        "source_ids": stable_unique_values(
            [*(existing.get("source_ids") or []), *(candidate.get("source_ids") or [])]
        ),
        "source_spans": stable_unique_dicts(
            [
                *(existing.get("source_spans") or []),
                *(candidate.get("source_spans") or []),
            ]
        ),
        "evidence_quote": candidate.get("evidence_quote")
        or existing.get("evidence_quote"),
        "evidence_unit_ids": candidate.get("evidence_unit_ids") or [],
        "extraction_confidence": max_optional_float(
            existing.get("extraction_confidence"),
            candidate.get("extraction_confidence"),
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


def extraction_window_prefilter_reason(window: dict[str, Any]) -> str | None:
    if not normalized_extraction_content(list(window.get("chunks") or [])):
        return "low_information_window"
    return None


def skipped_extraction_window_from_window(
    window: dict[str, Any], *, reason: str
) -> dict[str, Any]:
    return {
        "window_id": window["window_id"],
        "window_index": window.get("window_index"),
        "chunk_ids": [chunk["chunk_id"] for chunk in window.get("chunks") or []],
        "skipped": True,
        "reason": reason,
        "raw_fact_count": 0,
        "accepted_count": 0,
        "rejected_count": 0,
        "dropped_count": 0,
        "duration_ms": 0.0,
    }


def skipped_extraction_window(
    document_id: str,
    chunks: list[dict[str, Any]],
    *,
    reason: str,
) -> dict[str, Any]:
    return {
        "window_id": f"{document_id}:window:prefiltered",
        "window_index": None,
        "chunk_ids": [chunk["chunk_id"] for chunk in chunks],
        "skipped": True,
        "reason": reason,
        "raw_fact_count": 0,
        "accepted_count": 0,
        "rejected_count": 0,
        "dropped_count": 0,
        "duration_ms": 0.0,
    }


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
        "window": source_window_with_evidence_units(window),
        "routing_hints": routing_hints,
    }


def source_window_with_evidence_units(window: dict[str, Any]) -> dict[str, Any]:
    output = dict(window)
    output["chunks"] = [
        {key: value for key, value in chunk.items() if key != "text"}
        | {
            "units": [
                {
                    "unit_id": unit["unit_id"],
                    "text": unit["text"],
                    **({"speaker": unit["speaker"]} if unit.get("speaker") else {}),
                }
                for unit in evidence_units_for_text(str(chunk.get("text") or ""))
            ],
        }
        for chunk in window.get("chunks") or []
    ]
    return output


ROUTING_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9_/-]{1,}")
ROUTING_STOP_TOKENS = {
    "about",
    "after",
    "and",
    "answer",
    "answers",
    "before",
    "brain",
    "candidate",
    "career",
    "companies",
    "company",
    "concept",
    "concepts",
    "decision",
    "decisions",
    "details",
    "example",
    "facts",
    "for",
    "from",
    "here",
    "idea",
    "ideas",
    "into",
    "loops",
    "markdown",
    "notes",
    "open",
    "open_loops",
    "page",
    "pages",
    "participant",
    "people",
    "project",
    "projects",
    "questions",
    "reference",
    "related",
    "said",
    "says",
    "speaker",
    "summary",
    "target",
    "test",
    "that",
    "them",
    "their",
    "there",
    "they",
    "this",
    "using",
    "what",
    "when",
    "where",
    "with",
}


def normalize_extraction_page_hint(page_hint: str) -> str:
    normalized = canonical_page_hint_for_fact(str(page_hint or "").strip())
    while normalized.startswith("wiki/"):
        normalized = normalized.removeprefix("wiki/")
        normalized = canonical_page_hint_for_fact(normalized)
    return normalized


def indexed_extraction_page_hint(paths: BrainPaths, value: str) -> str:
    relative_path = indexed_page_relative_path(paths, value)
    if relative_path:
        return relative_path
    raw = str(value or "").strip()
    if "/wiki/" in raw:
        return raw.split("/wiki/", 1)[1]
    return raw


def route_namespace(page_hint: str) -> str:
    normalized = normalize_extraction_page_hint(page_hint)
    return normalized.split("/", 1)[0] if "/" in normalized else ""


def route_destination_review_reason(
    page_hint: str, *, page_type: str | None = None
) -> str | None:
    normalized = normalize_extraction_page_hint(page_hint)
    if not normalized:
        return "missing_page_hint"
    if normalized in DEFAULT_FALLBACK_PAGE_HINTS:
        return "fallback_page"
    if normalized.startswith("/") or "://" in normalized:
        return "non_wiki_relative_page_hint"
    if any(normalized.startswith(prefix) for prefix in INVALID_ROUTE_PREFIXES):
        return "non_canonical_route_namespace"
    if normalized == "index.md" or normalized.endswith("/index.md"):
        return "index_page_route"
    if page_type and str(page_type).casefold() in INVALID_ROUTE_PAGE_TYPES:
        return "non_canonical_page_type"
    if not normalized.endswith(".md"):
        return "non_markdown_page_hint"
    namespace = route_namespace(normalized)
    if namespace and namespace not in CANONICAL_ROUTE_NAMESPACES:
        return "unknown_canonical_namespace"
    return None


def route_fuzzy_key(page_hint: str) -> str:
    normalized = normalize_extraction_page_hint(page_hint)
    namespace = route_namespace(normalized)
    stem = normalized.removesuffix(".md").split("/")[-1]
    stem = re.sub(r"^\d{4}[-_]+", "", stem)
    return f"{namespace}:{compact_surface_key(stem)}"


def closest_route_target(
    page_hint: str, route_targets: dict[str, dict[str, Any]]
) -> tuple[str, float] | None:
    if not route_targets:
        return None
    normalized = normalize_extraction_page_hint(page_hint)
    namespace = route_namespace(normalized)
    source_key = route_fuzzy_key(normalized)
    best_page_hint = ""
    best_score = 0.0
    for target_page_hint in route_targets:
        target_namespace = route_namespace(target_page_hint)
        if namespace and target_namespace and namespace != target_namespace:
            continue
        target_key = route_fuzzy_key(target_page_hint)
        score = (
            1.0
            if source_key == target_key
            else SequenceMatcher(None, source_key, target_key).ratio()
        )
        if score > best_score:
            best_score = score
            best_page_hint = target_page_hint
    if best_page_hint and best_score >= ROUTE_FUZZY_DUP_THRESHOLD:
        return best_page_hint, best_score
    return None


def resolve_extraction_page_hint(
    raw_page_hint: str, route_targets: dict[str, dict[str, Any]]
) -> tuple[str, dict[str, Any]]:
    original = str(raw_page_hint or "concepts/extracted-facts.md").strip()
    normalized = normalize_extraction_page_hint(original)
    metadata: dict[str, Any] = {
        "original_page_hint": original,
        "normalized_page_hint": normalized,
        "route_destination_valid": True,
        "route_target_exists": False,
        "route_resolution": "new_canonical_page",
    }
    review_reason = route_destination_review_reason(normalized)
    if review_reason:
        metadata.update(
            {
                "route_destination_valid": False,
                "route_resolution": "held_for_routing_review",
                "route_review_reason": review_reason,
            }
        )
        return next(iter(sorted(DEFAULT_FALLBACK_PAGE_HINTS))), metadata
    if normalized in route_targets:
        metadata.update(
            {
                "route_target_exists": True,
                "route_resolution": "existing_canonical_page",
            }
        )
        return normalized, metadata
    closest = closest_route_target(normalized, route_targets)
    if closest is not None:
        snapped_page_hint, score = closest
        metadata.update(
            {
                "snapped_page_hint": snapped_page_hint,
                "route_target_exists": True,
                "route_resolution": "fuzzy_snapped_existing_page",
                "route_snap_score": round(score, 4),
            }
        )
        return snapped_page_hint, metadata
    return normalized, metadata


def load_extraction_route_targets(paths: BrainPaths) -> dict[str, dict[str, Any]]:
    with connection(paths.sqlite_path) as conn:
        hints_by_page: dict[str, dict[str, Any]] = {}
        for row in conn.execute(
            """
            SELECT path, title, page_type, updated_at, created_at, managed
            FROM wiki_pages
            WHERE status = 'active'
              AND COALESCE(managed, 0) = 1
            ORDER BY COALESCE(updated_at, created_at) DESC, path
            LIMIT ?
            """,
            (ROUTING_HINT_POOL_LIMIT,),
        ):
            page_hint = normalize_extraction_page_hint(
                indexed_extraction_page_hint(paths, str(row["path"] or ""))
            )
            if route_destination_review_reason(
                page_hint, page_type=str(row["page_type"] or "")
            ):
                continue
            hints_by_page[page_hint] = {
                "page_hint": page_hint,
                "canonical_entity": row["title"],
                "page_scope": row["page_type"],
                "retrieval_purpose": None,
                "_routing_source": "wiki_page",
                "_routing_updated_at": row["updated_at"] or row["created_at"] or "",
                "_route_target_exists": True,
                "_route_target_managed": bool(row["managed"]),
            }
        for row in conn.execute(
            """
            SELECT page_hint, canonical_entity, page_scope, retrieval_purpose,
                   updated_at, created_at
            FROM page_contracts
            WHERE status = 'active'
            ORDER BY COALESCE(updated_at, created_at) DESC, page_hint
            LIMIT ?
            """,
            (ROUTING_HINT_POOL_LIMIT,),
        ):
            page_hint = normalize_extraction_page_hint(str(row["page_hint"] or ""))
            if route_destination_review_reason(
                page_hint, page_type=str(row["page_scope"] or "")
            ):
                continue
            hints_by_page[page_hint] = {
                "page_hint": page_hint,
                "canonical_entity": row["canonical_entity"],
                "page_scope": row["page_scope"],
                "retrieval_purpose": row["retrieval_purpose"],
                "_routing_source": "page_contract",
                "_routing_updated_at": row["updated_at"] or row["created_at"] or "",
                "_route_target_exists": True,
                "_route_target_managed": True,
            }
    return hints_by_page


def load_extraction_routing_hint_pool(paths: BrainPaths) -> list[dict[str, Any]]:
    return [
        hint
        for hint in sorted(
            load_extraction_route_targets(paths).values(),
            key=lambda item: (
                str(item.get("_routing_updated_at") or ""),
                str(item.get("page_hint") or ""),
            ),
            reverse=True,
        )
    ]


def ranked_extraction_routing_hints(
    hint_pool: list[dict[str, Any]], query_text: str, *, limit: int
) -> list[dict[str, Any]]:
    if limit <= 0:
        return []
    query_tokens = routing_signal_tokens(query_text)
    ranked: list[tuple[float, str, str, dict[str, Any]]] = []
    for hint in hint_pool:
        hint_tokens = routing_signal_tokens(routing_hint_text(hint))
        overlap = query_tokens & hint_tokens
        source_bonus = 1.0 if hint.get("_routing_source") == "page_contract" else 0.0
        score = source_bonus
        if overlap:
            score += float(len(overlap) * 5)
            score += float(len(overlap)) / max(1.0, float(len(hint_tokens)))
            score += routing_phrase_bonus(query_text, hint)
        ranked.append(
            (
                score,
                str(hint.get("_routing_updated_at") or ""),
                str(hint.get("page_hint") or ""),
                public_routing_hint(hint),
            )
        )
    ranked.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)
    return [hint for score, _updated_at, _page_hint, hint in ranked[:limit]]


def routing_query_text(document: dict[str, Any], window: dict[str, Any]) -> str:
    parts = [
        str(document.get("title") or ""),
        str(document.get("source_type") or ""),
    ]
    for chunk in window.get("chunks") or []:
        parts.append(str(chunk.get("heading_path") or ""))
        parts.append(str(chunk.get("text") or ""))
    return "\n".join(part for part in parts if part)


def routing_hint_text(hint: dict[str, Any]) -> str:
    return "\n".join(
        str(hint.get(key) or "")
        for key in (
            "page_hint",
            "canonical_entity",
            "page_scope",
            "retrieval_purpose",
        )
    )


def routing_signal_tokens(value: str) -> set[str]:
    tokens: set[str] = set()
    for raw in ROUTING_TOKEN_RE.findall(value.casefold().replace("-", " ")):
        for token in re.split(r"[/_\s]+", raw):
            if len(token) < 3 or token in ROUTING_STOP_TOKENS:
                continue
            tokens.add(token)
    return tokens


def routing_phrase_bonus(query_text: str, hint: dict[str, Any]) -> float:
    query_key = compact_surface_key(query_text)
    bonus = 0.0
    for key in ("canonical_entity", "page_hint"):
        surface = str(hint.get(key) or "").removesuffix(".md").split("/")[-1]
        surface_key = compact_surface_key(surface)
        if surface_key and surface_key in query_key:
            bonus += 3.0
    return bonus


def public_routing_hint(hint: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in hint.items()
        if not key.startswith("_") and value is not None
    }


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
                            "normalized_content_empty": bool(
                                document.get("normalized_content_empty")
                            ),
                            "validation": validation or {},
                        }
                    ),
                ),
            )


def extraction_prompt(source_window: dict[str, Any]) -> str:
    return (
        "Extract atomic source-backed facts from this single source window.\n"
        "Each fact must include: statement, chunk_id, evidence_unit_ids, page_hint, section_hint, "
        "claim_class, entities, extraction_confidence, routing_confidence, and truth_confidence. "
        "Also include entity_key as the primary entity surface string when available.\n"
        "claim_class must be one of: decision, commitment, preference, role_or_responsibility, "
        "project_state, factual_update, open_question, event_metadata, transcript_mechanic, "
        "pleasantry, boilerplate, non_claim.\n"
        "entities must be a list of extracted entity mentions. Each mention must include surface, "
        "type, mention_kind, and is_primary. type must be one of: "
        f"{', '.join(sorted(ENTITY_TYPES))}. mention_kind must be one of: "
        f"{', '.join(sorted(MENTION_KINDS))}. Use mention_kind='named' for a specific person, "
        "company, product, project, place, event, or other proper referent; 'concept' for a durable "
        "technical/topic concept; 'generic' for role classes/common nouns like engineers or customers; "
        "and 'deictic' for context-relative phrases like our team, their partner, or this group. "
        "Mark exactly one primary entity for each fact.\n"
        "Only propose durable claims worth future retrieval. Omit event metadata, transcript mechanics, "
        "pleasantries, boilerplate, and non-claims when possible. If you include one, label it accurately; "
        "deterministic policy will drop it.\n"
        "Statement support rules:\n"
        "- Every part of the statement must be directly entailed by the cited evidence units. Do not add "
        "reasonable background knowledge, implications, titles, customer/investor impact, active-application "
        "status, locations, or causal explanations unless the cited unit text says them.\n"
        "- Keep statements atomic. If the units support only one side of a combined claim, return only the "
        "supported side; do not join it with an unsupported inference.\n"
        "- Preserve uncertainty and negation from the source. If the source says a speaker was confused, unsure, "
        "or not continuing a process, the statement must say that rather than smoothing it into a clean fact.\n"
        "- Transcript unit speaker labels are stable within one source document. Attribute a statement to a named "
        "person only when the source window establishes that the labeled speaker is that person through a "
        "self-introduction or direct address. A participant list alone does not establish speaker identity; use the "
        "Speaker N label when identity is unresolved.\n"
        "Evidence rules:\n"
        "- Use the exact chunk_id string from the source card.\n"
        '- Cite evidence by evidence_unit_ids from that chunk\'s units array, such as ["u3"] or ["u3", "u4"].\n'
        f"- Cite between 1 and {MAX_EVIDENCE_UNITS_PER_FACT} evidence units per fact.\n"
        "- Prefer the smallest set of units that directly supports the statement.\n"
        "- Do not return evidence_quote, source_spans, or character offsets; deterministic code reconstructs "
        "quotes and spans from the cited unit ids.\n"
        "- Do not cite a unit unless its text supports the statement; if no unit supports a fact, omit that fact.\n"
        "Routing rules:\n"
        "- page_hint must be a wiki-relative markdown path such as projects/example.md or concepts/example.md.\n"
        "- Prefer one of the provided routing_hints when a hint fits the fact.\n"
        "- Treat the source document's title and dominant topic as a routing prior: facts from one "
        "conversation usually belong with other facts from that conversation. Keep a fact on a "
        "different page when its own evidence clearly changes topic; document coherence is a preference, "
        "not an absolute rule.\n"
        "- Use concepts/extracted-facts.md only when no canonical routing target fits.\n"
        "- Never use references/*.md, wiki/references/*.md, agent_session_log pages, absolute file paths, "
        "raw source paths, or docs/*.md audit file paths as page_hint.\n\n"
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
        "For every returned fact, cite evidence_unit_ids from the named chunk's units array. "
        f"Use 1 to {MAX_EVIDENCE_UNITS_PER_FACT} unit ids. Do not return evidence_quote or source_spans.\n\n"
        "Every corrected statement must be directly entailed by the cited units. Drop or narrow any fact that "
        "adds an unsupported title, role, business impact, location, active-process status, or causal explanation. "
        "Preserve negation and uncertainty exactly when the source is ambiguous or says a process is not continuing.\n\n"
        "For transcript facts, keep the source speaker label unless the source window directly establishes the "
        "speaker's name. Do not infer speaker identity from a participant list alone.\n\n"
        "For every returned entity mention, include mention_kind as one of named, concept, generic, deictic. "
        "Use generic/deictic rather than forcing role classes or speaker-relative phrases into named entities.\n\n"
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
        require_model_confidence=True,
    )


def validate_extracted_facts_with_report(
    paths: BrainPaths,
    raw_facts: list[Any],
    *,
    extractor_model: str | None = None,
    require_model_confidence: bool = False,
) -> dict[str, Any]:
    chunk_context_by_id = load_chunk_contexts(paths)
    route_targets = load_extraction_route_targets(paths)
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
        evidence_ref, evidence_ref_errors = fact_evidence_ref(item)
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
        if low_value_fact_statement(statement):
            dropped.append(
                {
                    "index": index,
                    "statement": clip_text(statement),
                    "claim_class": claim_class,
                    "reason": "low_value_placeholder_fact",
                }
            )
            continue
        confidence_values, confidence_errors = extraction_confidence_values(
            item, require_all=require_model_confidence
        )
        reasons.extend(confidence_errors)
        reasons.extend(evidence_ref_errors)
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
        evidence_unit_refs = []
        evidence_reasons: list[str] = []
        if evidence_ref is not None:
            resolved = resolve_evidence_units(chunk_context_by_id, evidence_ref)
            valid_spans.extend(resolved["source_spans"])
            valid_quotes.extend(resolved["quotes"])
            source_ids.extend(resolved["source_ids"])
            evidence_unit_refs.extend(resolved["evidence_units"])
            evidence_reasons.extend(resolved["reasons"])
        if not valid_spans:
            rejections.append(
                {
                    "index": index,
                    "statement": clip_text(statement),
                    "reasons": [
                        *reasons,
                        *(evidence_reasons or ["no valid evidence_unit_ids"]),
                    ],
                }
            )
            continue
        page_hint, route_metadata = resolve_extraction_page_hint(
            str(item.get("page_hint") or "concepts/extracted-facts.md"),
            route_targets,
        )
        section_hint = str(item.get("section_hint") or "Summary")
        entity_mentions, entity_errors = normalize_extracted_entity_mentions(
            item,
            valid_spans,
            chunk_context_by_id,
        )
        if entity_errors:
            rejections.append(
                {
                    "index": index,
                    "statement": clip_text(statement),
                    "reasons": entity_errors,
                }
            )
            continue
        faithfulness_reasons = statement_faithfulness_reasons(
            statement,
            "\n".join(valid_quotes),
            entity_mentions,
        )
        if faithfulness_reasons:
            rejections.append(
                {
                    "index": index,
                    "statement": clip_text(statement),
                    "reasons": faithfulness_reasons,
                }
            )
            continue
        model_entity_key = primary_entity_surface(item, entity_mentions)
        primary_type = primary_entity_type(entity_mentions)
        entity_key = entity_key_for_change(
            topic_for_path(page_hint), page_hint, section_hint
        )
        candidate = {
            "statement": statement,
            "entity_key": entity_key,
            "page_hint": page_hint,
            "section_hint": section_hint,
            "claim_class": claim_class,
            "source_ids": sorted(set(source_ids)),
            "source_spans": valid_spans,
            "evidence_quote": "\n...\n".join(valid_quotes)[:1000]
            if valid_quotes
            else None,
            "evidence_unit_ids": [unit["unit_id"] for unit in evidence_unit_refs],
            "observed_at": item.get("observed_at"),
            "effective_at": item.get("effective_at"),
            "confidence": confidence_values["truth_confidence"],
            "extraction_confidence": confidence_values["extraction_confidence"],
            "routing_confidence": confidence_values["routing_confidence"],
            "truth_confidence": confidence_values["truth_confidence"],
            "extraction_method": "llm",
            "extractor_model": item.get("extractor_model") or extractor_model,
            "metadata": {
                "source": "source_to_facts_extraction",
                "claim_class": claim_class,
                "evidence_units": evidence_unit_refs,
                "routing": route_metadata,
                **(
                    {
                        "evidence_unit_truncation": {
                            "original_count": evidence_ref["original_unit_count"],
                            "kept_count": len(evidence_ref["unit_ids"]),
                            "truncated_count": evidence_ref["truncated_unit_count"],
                        }
                    }
                    if evidence_ref
                    and int(evidence_ref.get("truncated_unit_count") or 0) > 0
                    else {}
                ),
                **({"model_entity_key": model_entity_key} if model_entity_key else {}),
                **(
                    {"model_entity_mentions": entity_mentions}
                    if entity_mentions
                    else {}
                ),
            },
        }
        if model_entity_key:
            candidate["entity_mention"] = model_entity_key
        if primary_type:
            candidate["entity_type"] = primary_type
        if entity_mentions:
            candidate["entity_mentions"] = entity_mentions
        candidates.append(candidate)
    return {
        "candidates": candidates,
        "raw_fact_count": len(raw_facts),
        "accepted_count": len(candidates),
        "rejected_count": len(rejections),
        "dropped_count": len(dropped),
        **route_validation_metrics(candidates),
        "schema_errors": [],
        "rejections": rejections,
        "dropped": dropped,
    }


def fact_evidence_ref(item: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
    errors: list[str] = []
    chunk_id = str(item.get("chunk_id") or "").strip()
    raw_unit_ids = item.get("evidence_unit_ids")
    if not chunk_id:
        errors.append("missing chunk_id")
    if not isinstance(raw_unit_ids, list):
        errors.append("missing evidence_unit_ids")
        return None, errors
    unit_ids = stable_unique_strings([str(unit_id).strip() for unit_id in raw_unit_ids])
    if not unit_ids:
        errors.append("missing evidence_unit_ids")
    if errors:
        return None, errors
    selected_unit_ids = unit_ids[:MAX_EVIDENCE_UNITS_PER_FACT]
    return {
        "chunk_id": chunk_id,
        "unit_ids": selected_unit_ids,
        "original_unit_count": len(unit_ids),
        "truncated_unit_count": max(0, len(unit_ids) - len(selected_unit_ids)),
    }, []


def resolve_evidence_units(
    chunk_context_by_id: dict[str, dict[str, Any]],
    evidence_ref: dict[str, Any],
) -> dict[str, Any]:
    chunk_id = str(evidence_ref.get("chunk_id") or "").strip()
    unit_ids = [str(unit_id).strip() for unit_id in evidence_ref.get("unit_ids") or []]
    chunk_context = chunk_context_by_id.get(chunk_id)
    if chunk_context is None:
        return empty_resolved_evidence([f"unknown chunk_id: {clip_text(chunk_id, 80)}"])
    text = str(chunk_context["text"])
    resolved = resolve_evidence_unit_ids(
        text,
        chunk_id=chunk_id,
        unit_ids=unit_ids,
    )
    missing = resolved["missing_unit_ids"]
    if missing:
        return empty_resolved_evidence(
            [
                f"unknown evidence_unit_id for {clip_text(chunk_id, 80)}: "
                f"{', '.join(missing)}"
            ]
        )
    return {
        "source_spans": resolved["source_spans"],
        "quotes": resolved["quotes"],
        "source_ids": resolved["source_ids"],
        "evidence_units": resolved["evidence_units"],
        "reasons": [],
    }


def empty_resolved_evidence(reasons: list[str]) -> dict[str, Any]:
    return {
        "source_spans": [],
        "quotes": [],
        "source_ids": [],
        "evidence_units": [],
        "reasons": reasons,
    }


def statement_faithfulness_reasons(
    statement: str,
    evidence_text: str,
    entity_mentions: list[dict[str, Any]],
) -> list[str]:
    reasons: list[str] = []
    unsupported_numbers = unsupported_statement_numbers(statement, evidence_text)
    if unsupported_numbers:
        reasons.append(
            "statement_not_supported_by_evidence: unsupported number(s): "
            + ", ".join(unsupported_numbers)
        )
    return reasons


def compact_surface_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.casefold())


NUMBER_TOKEN_RE = re.compile(r"\$?\d+(?:,\d{3})*(?:\.\d+)?(?:[%kKmMbB])?|[A-Za-z]+")
NUMBER_WORD_VALUES = {
    "zero": 0,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
    "twenty": 20,
    "thirty": 30,
    "forty": 40,
    "fifty": 50,
    "sixty": 60,
    "seventy": 70,
    "eighty": 80,
    "ninety": 90,
}
NUMBER_SCALE_WORDS = {
    "hundred": 100,
    "thousand": 1_000,
    "k": 1_000,
    "million": 1_000_000,
    "m": 1_000_000,
    "billion": 1_000_000_000,
    "bn": 1_000_000_000,
    "b": 1_000_000_000,
}


def unsupported_statement_numbers(statement: str, evidence_text: str) -> list[str]:
    statement_numbers = extract_numeric_mentions(statement)
    if not statement_numbers:
        return []
    evidence_numbers = extract_numeric_mentions(evidence_text)
    unsupported: list[str] = []
    for statement_number in statement_numbers:
        if not any(
            number_values_match(statement_number, evidence_number)
            for evidence_number in evidence_numbers
        ):
            unsupported.append(statement_number["surface"])
    return stable_unique_strings(unsupported)


def extract_numeric_mentions(text: str) -> list[dict[str, Any]]:
    mentions: list[dict[str, Any]] = []
    tokens = [
        (match.group(0), match.start(), match.end())
        for match in NUMBER_TOKEN_RE.finditer(text)
    ]
    consumed_word_indexes: set[int] = set()
    for index, (raw, start, end) in enumerate(tokens):
        if re.search(r"\d", raw) and digit_token_is_identifierish(text, start, end):
            continue
        parsed = parse_digit_number_token(
            raw, tokens[index + 1][0] if index + 1 < len(tokens) else ""
        )
        if parsed is not None:
            value, kind = parsed
            mentions.append({"surface": raw, "value": value, "kind": kind})
            continue
        if index in consumed_word_indexes:
            continue
        parsed_words = parse_number_word_sequence(tokens, index)
        if parsed_words is None:
            continue
        value, kind, end_index = parsed_words
        if number_word_sequence_is_idiom(tokens, index, end_index):
            continue
        consumed_word_indexes.update(range(index, end_index + 1))
        surface = text[start : tokens[end_index][2]]
        mentions.append({"surface": surface, "value": value, "kind": kind})
    return mentions


def digit_token_is_identifierish(text: str, start: int, end: int) -> bool:
    before = text[start - 1] if start > 0 else ""
    after = text[end] if end < len(text) else ""
    if before.isalnum() or after.isalnum():
        return True
    if before == "/" or after == "/":
        return True
    if before == "-" and any(
        char.isalpha() for char in text[max(0, start - 12) : start]
    ):
        return True
    if after == "-" and any(
        char.isalpha() for char in text[end : min(len(text), end + 12)]
    ):
        return True
    return False


def parse_digit_number_token(raw: str, next_token: str) -> tuple[float, str] | None:
    token = raw.strip()
    has_currency = token.startswith("$")
    token = token.lstrip("$").replace(",", "")
    suffix = ""
    if token and token[-1] in "%kKmMbB":
        suffix = token[-1].lower()
        token = token[:-1]
    if not re.fullmatch(r"\d+(?:\.\d+)?", token):
        return None
    value = float(token)
    kind = "number"
    if suffix == "%":
        kind = "percent"
    elif suffix in {"k", "m", "b"}:
        value *= {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000}[suffix]
    else:
        scale = NUMBER_SCALE_WORDS.get(next_token.casefold())
        if scale and scale >= 1_000:
            value *= scale
    if has_currency:
        kind = "number"
    return value, kind


def parse_number_word_sequence(
    tokens: list[tuple[str, int, int]],
    start_index: int,
) -> tuple[float, str, int] | None:
    total = 0.0
    current = 0.0
    index = start_index
    consumed = False
    kind = "number"
    while index < len(tokens):
        word = tokens[index][0].casefold().replace("-", " ")
        if word in {"and", "a"} and consumed:
            index += 1
            continue
        if word == "point" and consumed:
            decimal_digits: list[str] = []
            index += 1
            while index < len(tokens):
                digit_word = tokens[index][0].casefold()
                if (
                    digit_word not in NUMBER_WORD_VALUES
                    or NUMBER_WORD_VALUES[digit_word] > 9
                ):
                    break
                decimal_digits.append(str(NUMBER_WORD_VALUES[digit_word]))
                index += 1
            if decimal_digits:
                current += float("0." + "".join(decimal_digits))
                continue
            break
        if word in NUMBER_WORD_VALUES:
            current += NUMBER_WORD_VALUES[word]
            consumed = True
            index += 1
            continue
        if word == "half" and consumed:
            current += 0.5
            index += 1
            continue
        if word in NUMBER_SCALE_WORDS and consumed:
            scale = NUMBER_SCALE_WORDS[word]
            if scale == 100:
                current = max(1.0, current) * scale
            else:
                total += max(1.0, current) * scale
                current = 0.0
            consumed = True
            index += 1
            continue
        if word in {"percent", "percentage"} and consumed:
            kind = "percent"
            index += 1
            break
        break
    if not consumed:
        return None
    return total + current, kind, index - 1


def number_word_sequence_is_idiom(
    tokens: list[tuple[str, int, int]],
    start_index: int,
    end_index: int,
) -> bool:
    words = [tokens[index][0].casefold() for index in range(start_index, end_index + 1)]
    previous_words = [
        tokens[index][0].casefold()
        for index in range(max(0, start_index - 2), start_index)
    ]
    next_words = [
        tokens[index][0].casefold()
        for index in range(end_index + 1, min(len(tokens), end_index + 3))
    ]
    if words == ["zero"] and next_words[:2] == ["to", "one"]:
        return True
    if previous_words[-2:] == ["zero", "to"] and words == ["one"]:
        return True
    if words == ["one"] and next_words[:2] == ["on", "one"]:
        return True
    if previous_words[-2:] == ["one", "on"] and words == ["one"]:
        return True
    if previous_words[-1:] == ["day"] and words == ["one"]:
        return True
    return False


def number_values_match(left: dict[str, Any], right: dict[str, Any]) -> bool:
    if left.get("kind") != right.get("kind"):
        return False
    left_value = float(left.get("value") or 0.0)
    right_value = float(right.get("value") or 0.0)
    tolerance = max(1.0, abs(left_value) * 0.05)
    return abs(left_value - right_value) <= tolerance


def normalize_extracted_entity_mentions(
    item: dict[str, Any],
    valid_spans: list[dict[str, Any]],
    chunk_context_by_id: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    raw_mentions = item.get("entities")
    if raw_mentions is None:
        raw_mentions = item.get("entity_mentions")
    if raw_mentions is None:
        return [], []
    if not isinstance(raw_mentions, list):
        return [], ["entities must be an array"]
    mentions: list[dict[str, Any]] = []
    errors: list[str] = []
    primary_seen = False
    for index, raw in enumerate(raw_mentions):
        if not isinstance(raw, dict):
            errors.append(f"entities[{index}] must be an object")
            continue
        surface = str(
            raw.get("surface")
            or raw.get("mention")
            or raw.get("name")
            or raw.get("entity_key")
            or ""
        ).strip()
        if not surface:
            errors.append(f"entities[{index}].surface is required")
            continue
        raw_type = (
            raw.get("type") if raw.get("type") is not None else raw.get("entity_type")
        )
        entity_type = normalize_entity_type(str(raw_type or ""))
        if entity_type is None:
            errors.append(
                f"entities[{index}].type must be one of: {', '.join(sorted(ENTITY_TYPES))}"
            )
            continue
        raw_kind = (
            raw.get("mention_kind")
            if raw.get("mention_kind") is not None
            else raw.get("kind")
        )
        mention_kind = normalize_mention_kind(raw_kind)
        if raw_kind is not None and mention_kind is None:
            errors.append(
                f"entities[{index}].mention_kind must be one of: "
                f"{', '.join(sorted(MENTION_KINDS))}"
            )
            continue
        is_primary = raw_entity_mention_is_primary(raw) and not primary_seen
        if is_primary:
            primary_seen = True
        mentions.append(
            {
                "surface": surface,
                "entity_type": entity_type,
                "mention_kind": mention_kind,
                "is_primary": is_primary,
                "mention_span": derive_mention_span(
                    surface, raw, valid_spans, chunk_context_by_id
                ),
                "confidence": optional_float(raw.get("confidence")),
            }
        )
    if errors:
        return [], errors
    if mentions and not any(mention["is_primary"] for mention in mentions):
        mentions[0]["is_primary"] = True
    return dedupe_entity_mentions(mentions), []


def raw_entity_mention_is_primary(raw: dict[str, Any]) -> bool:
    if "is_primary" in raw:
        return bool(raw.get("is_primary"))
    role = str(raw.get("role") or "").strip().lower()
    return role in {"primary", "main", "subject"}


def derive_mention_span(
    surface: str,
    raw: dict[str, Any],
    valid_spans: list[dict[str, Any]],
    chunk_context_by_id: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    chunk_ids = stable_unique_strings(
        [
            str(raw.get("chunk_id") or "").strip(),
            *[str(span.get("chunk_id") or "").strip() for span in valid_spans],
        ]
    )
    for chunk_id in chunk_ids:
        chunk_context = chunk_context_by_id.get(chunk_id)
        if chunk_context is None:
            continue
        span = find_quote_span(str(chunk_context["text"]), surface)
        if span is None:
            span = find_casefold_span(str(chunk_context["text"]), surface)
        if span is not None:
            start, end = span
            return {"chunk_id": chunk_id, "start": start, "end": end}
    return None


def find_casefold_span(text: str, quote: str) -> tuple[int, int] | None:
    stripped = quote.strip()
    if not stripped:
        return None
    start = text.casefold().find(stripped.casefold())
    if start < 0:
        return None
    return start, start + len(stripped)


def dedupe_entity_mentions(mentions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    seen: set[tuple[str, str | None]] = set()
    for mention in mentions:
        key = (
            normalize_entity_name(mention.get("surface")),
            mention.get("entity_type"),
            mention.get("mention_kind"),
        )
        if key in seen:
            continue
        seen.add(key)
        output.append(mention)
    if output and not any(mention["is_primary"] for mention in output):
        output[0]["is_primary"] = True
    return output


def primary_entity_surface(item: dict[str, Any], mentions: list[dict[str, Any]]) -> str:
    for mention in mentions:
        if mention.get("is_primary"):
            return str(mention.get("surface") or "").strip()
    return str(item.get("entity_key") or item.get("entity_mention") or "").strip()


def primary_entity_type(mentions: list[dict[str, Any]]) -> str | None:
    for mention in mentions:
        if mention.get("is_primary"):
            return normalize_entity_type(mention.get("entity_type"))
    return None


def stable_unique_strings(values: list[str]) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not value or value in seen:
            continue
        output.append(value)
        seen.add(value)
    return output


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


def route_validation_metrics(candidates: list[dict[str, Any]]) -> dict[str, Any]:
    resolution_counts: dict[str, int] = {}
    canonical_count = 0
    fallback_count = 0
    invalid_destination_count = 0
    existing_target_count = 0
    new_page_count = 0
    snapped_count = 0
    for candidate in candidates:
        page_hint = normalize_extraction_page_hint(
            str(candidate.get("page_hint") or "")
        )
        routing = candidate_route_metadata(candidate)
        resolution = str(routing.get("route_resolution") or "unknown")
        resolution_counts[resolution] = resolution_counts.get(resolution, 0) + 1
        if page_hint in DEFAULT_FALLBACK_PAGE_HINTS:
            fallback_count += 1
        if routing.get("route_destination_valid") is False:
            invalid_destination_count += 1
        if routing.get("route_target_exists") is True:
            existing_target_count += 1
        if resolution == "new_canonical_page":
            new_page_count += 1
        if resolution == "fuzzy_snapped_existing_page":
            snapped_count += 1
        if (
            page_hint not in DEFAULT_FALLBACK_PAGE_HINTS
            and routing.get("route_destination_valid") is not False
        ):
            canonical_count += 1
    accepted_count = len(candidates)
    return {
        "canonical_route_count": canonical_count,
        "canonical_route_rate": round(canonical_count / accepted_count, 4)
        if accepted_count
        else 0.0,
        "fallback_count": fallback_count,
        "invalid_route_destination_count": invalid_destination_count,
        "existing_route_target_count": existing_target_count,
        "new_canonical_route_count": new_page_count,
        "fuzzy_snapped_route_count": snapped_count,
        "route_resolution_counts": dict(sorted(resolution_counts.items())),
    }


def aggregate_document_validation(
    document: dict[str, Any],
    window_validations: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
) -> dict[str, Any]:
    skipped_windows = list(document.get("skipped_windows") or [])
    return {
        "document_id": document["document_id"],
        "source_type": document["source_type"],
        "window_count": len(window_validations),
        "source_window_count": len(window_validations) + len(skipped_windows),
        "skipped_window_count": len(skipped_windows),
        "raw_fact_count": sum(
            int(item.get("raw_fact_count") or 0) for item in window_validations
        ),
        "accepted_count": len(candidates),
        "rejected_count": sum(
            int(item.get("rejected_count") or 0) for item in window_validations
        ),
        "dropped_count": sum(
            int(item.get("dropped_count") or 0) for item in window_validations
        ),
        **route_validation_metrics(candidates),
        "attempt_count": sum(
            int(item.get("attempt_count") or len(item.get("attempts") or []))
            for item in window_validations
        ),
        "duration_ms": round(
            sum(float(item.get("duration_ms") or 0.0) for item in window_validations), 3
        ),
        "llm_duration_ms": round(
            sum(
                float(item.get("llm_duration_ms") or 0.0) for item in window_validations
            ),
            3,
        ),
        "validation_duration_ms": round(
            sum(
                float(item.get("validation_duration_ms") or 0.0)
                for item in window_validations
            ),
            3,
        ),
        "prompt_char_count": sum(
            int(item.get("prompt_char_count") or 0) for item in window_validations
        ),
        "source_window_char_count": sum(
            int(item.get("source_window_char_count") or 0)
            for item in window_validations
        ),
        "total_rejected_count": sum(
            int(item.get("total_rejected_count") or item.get("rejected_count") or 0)
            for item in window_validations
        ),
        "schema_errors": [
            error
            for item in window_validations
            for error in (item.get("schema_errors") or [])
        ],
        "rejections": [
            rejection
            for item in window_validations
            for rejection in (item.get("rejections") or [])
        ][:8],
        "dropped": [
            dropped
            for item in window_validations
            for dropped in (item.get("dropped") or [])
        ][:8],
        "windows": window_validations,
        "skipped_windows": skipped_windows,
    }


def aggregate_run_validation(
    document_validations: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "document_count": len(document_validations),
        "window_count": sum(
            int(item.get("window_count") or 0) for item in document_validations
        ),
        "source_window_count": sum(
            int(item.get("source_window_count") or 0) for item in document_validations
        ),
        "skipped_window_count": sum(
            int(item.get("skipped_window_count") or 0) for item in document_validations
        ),
        "raw_fact_count": sum(
            int(item.get("raw_fact_count") or 0) for item in document_validations
        ),
        "accepted_count": len(candidates),
        "rejected_count": sum(
            int(item.get("rejected_count") or 0) for item in document_validations
        ),
        "dropped_count": sum(
            int(item.get("dropped_count") or 0) for item in document_validations
        ),
        **route_validation_metrics(candidates),
        "attempt_count": sum(
            int(item.get("attempt_count") or 0) for item in document_validations
        ),
        "llm_duration_ms": round(
            sum(
                float(item.get("llm_duration_ms") or 0.0)
                for item in document_validations
            ),
            3,
        ),
        "validation_duration_ms": round(
            sum(
                float(item.get("validation_duration_ms") or 0.0)
                for item in document_validations
            ),
            3,
        ),
        "prompt_char_count": sum(
            int(item.get("prompt_char_count") or 0) for item in document_validations
        ),
        "source_window_char_count": sum(
            int(item.get("source_window_char_count") or 0)
            for item in document_validations
        ),
        "total_rejected_count": sum(
            int(item.get("total_rejected_count") or item.get("rejected_count") or 0)
            for item in document_validations
        ),
        "schema_errors": [
            error
            for item in document_validations
            for error in (item.get("schema_errors") or [])
        ],
        "rejections": [
            rejection
            for item in document_validations
            for rejection in (item.get("rejections") or [])
        ][:8],
        "dropped": [
            dropped
            for item in document_validations
            for dropped in (item.get("dropped") or [])
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
    for key in (
        "duration_ms",
        "llm_duration_ms",
        "validation_duration_ms",
        "prompt_char_count",
        "source_window_char_count",
        "canonical_route_count",
        "canonical_route_rate",
        "fallback_count",
        "invalid_route_destination_count",
        "existing_route_target_count",
        "new_canonical_route_count",
        "fuzzy_snapped_route_count",
        "route_resolution_counts",
    ):
        if key in report:
            output[key] = report[key]
    if attempt is not None:
        output["attempt"] = attempt
    return output


def better_extraction_report(
    candidate: dict[str, Any], current: dict[str, Any]
) -> bool:
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
    if report.get("schema_errors"):
        return True
    return any(
        extraction_rejection_is_retryable(rejection)
        for rejection in report.get("rejections") or []
    )


def extraction_rejection_is_retryable(rejection: dict[str, Any]) -> bool:
    reasons = [str(reason) for reason in rejection.get("reasons") or []]
    if not reasons:
        return True
    return any(
        not reason.startswith("statement_not_supported_by_evidence")
        for reason in reasons
    )


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
    if (
        validation.get("schema_errors")
        or int(validation.get("rejected_count") or 0) > 0
    ):
        return "invalid"
    if candidates:
        return "ok"
    return "extracted_empty"


def normalize_claim_class(value: Any) -> str | None:
    normalized = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    return normalized or None


def normalized_extraction_content_hash(chunks: list[dict[str, Any]]) -> str:
    normalized = normalized_extraction_content(chunks)
    return f"normalized:{hashlib.sha256(normalized.encode('utf-8')).hexdigest()}"


def normalized_extraction_content(chunks: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    skip_section = False
    for chunk in chunks:
        for raw_line in str(chunk.get("text") or "").splitlines():
            line = re.sub(r"\s+", " ", raw_line.strip())
            if not line:
                continue
            heading_match = re.match(r"^#{1,6}\s+(.+)$", line)
            if heading_match:
                skip_section = heading_match.group(1).strip().casefold() in {
                    "known participants"
                }
                continue
            if skip_section:
                continue
            if low_information_line(line):
                continue
            lines.append(line.lower())
    return "\n".join(lines)


def low_information_line(line: str) -> bool:
    return any(pattern.match(line) for pattern in LOW_INFORMATION_LINE_PATTERNS)


def low_value_fact_statement(statement: str) -> bool:
    normalized = re.sub(r"\s+", " ", statement.strip())
    return any(
        pattern.match(normalized) for pattern in LOW_VALUE_FACT_STATEMENT_PATTERNS
    )


def elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 3)


def extraction_run_timing(
    started: float,
    *,
    selection_duration_ms: float = 0.0,
    routing_hints_duration_ms: float = 0.0,
    extraction_duration_ms: float = 0.0,
    apply_duration_ms: float = 0.0,
    worker_count: int = DEFAULT_EXTRACTION_MAX_WORKERS,
) -> dict[str, Any]:
    return {
        "duration_ms": elapsed_ms(started),
        "selection_duration_ms": selection_duration_ms,
        "routing_hints_duration_ms": routing_hints_duration_ms,
        "extraction_duration_ms": extraction_duration_ms,
        "apply_duration_ms": apply_duration_ms,
        "worker_count": worker_count,
    }


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
