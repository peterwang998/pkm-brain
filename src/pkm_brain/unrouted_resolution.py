from __future__ import annotations

import json
import re
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
from difflib import SequenceMatcher
from typing import Any

from .curation_settings import load_curation_settings
from .db import connection, loads
from .llm import (
    LLMProvider,
    LLMProviderError,
    complete_json,
    cos_role_provider_configured,
)
from .paths import BrainPaths
from .routing_coherence import load_document_route_priors
from .source_dates import document_source_date_metadata
from .util import slugify
from .wiki_facts import (
    canonical_page_hint_for_fact,
    entity_key_for_change,
    topic_for_path,
)

FALLBACK_PAGE_HINTS = {"concepts/extracted-facts.md"}
INVALID_ROUTE_PREFIXES = (
    "agent_session_log/",
    "config/",
    "db/",
    "docs/",
    "inbox/",
    "raw/",
    "references/",
    "wiki/references/",
)
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
ROUTE_RESOLVER_BATCH_SIZE = 6
ROUTE_RESOLVER_MAX_WORKERS = 4
ROUTE_RESOLVER_OUTPUT_ATTEMPTS = 3
ROUTE_RESOLVER_SINGLE_OUTPUT_ATTEMPTS = 2
ROUTE_RESOLVER_CANDIDATE_LIMIT = 10
ROUTE_RESOLVER_SIBLING_LIMIT = 12
ROUTE_FUZZY_DUP_THRESHOLD = 0.92
ROUTE_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9_-]{1,}")
ROUTE_STOP_TOKENS = {
    "about",
    "after",
    "and",
    "candidate",
    "concepts",
    "facts",
    "from",
    "into",
    "page",
    "project",
    "said",
    "speaker",
    "summary",
    "that",
    "their",
    "this",
    "with",
}


def resolve_unrouted_candidate_routes(
    paths: BrainPaths,
    candidates: list[dict[str, Any]],
    route_targets: dict[str, dict[str, Any]],
    *,
    llm_provider: LLMProvider | None = None,
    provider: str | None = None,
    min_confidence: float | None = None,
    usage_cycle_id: str | None = None,
    usage_run_id: str | None = None,
    usage_stage: str | None = None,
) -> list[dict[str, Any]]:
    effective_min_confidence = (
        float(min_confidence)
        if min_confidence is not None
        else float(load_curation_settings(paths)["minimum_auto_confidence"])
    )
    output = [deepcopy(candidate) for candidate in candidates]
    pending_indexes = [
        index
        for index, candidate in enumerate(output)
        if candidate_requires_route_resolution(candidate)
    ]
    if not pending_indexes or not cos_role_provider_configured(
        paths, "resolver", llm_provider=llm_provider, provider=provider
    ):
        return output
    context = load_route_resolution_context(paths, output, route_targets)
    batches = batched(pending_indexes, ROUTE_RESOLVER_BATCH_SIZE)
    decisions_by_batch = resolve_route_batches(
        paths,
        output,
        route_targets,
        context,
        batches,
        llm_provider=llm_provider,
        provider=provider,
        usage_cycle_id=usage_cycle_id,
        usage_run_id=usage_run_id,
        usage_stage=usage_stage,
    )
    for batch in batches:
        decisions = decisions_by_batch.get(batch[0], {})
        for index in batch:
            decision = decisions.get(index)
            if decision is None:
                output[index] = mark_unresolved_route_decision(
                    output[index], None, resolution="resolver_no_decision"
                )
                continue
            routed = apply_route_resolution_decision(
                output[index],
                decision,
                route_targets,
                min_confidence=effective_min_confidence,
            )
            if routed is not None:
                output[index] = routed
            else:
                output[index] = mark_unresolved_route_decision(
                    output[index], decision, resolution="resolver_requires_human"
                )
    return output


def resolve_route_batches(
    paths: BrainPaths,
    candidates: list[dict[str, Any]],
    route_targets: dict[str, dict[str, Any]],
    context: dict[str, Any],
    batches: list[list[int]],
    *,
    llm_provider: LLMProvider | None,
    provider: str | None,
    usage_cycle_id: str | None,
    usage_run_id: str | None,
    usage_stage: str | None,
) -> dict[int, dict[int, dict[str, Any]]]:
    def resolve(batch: list[int]) -> dict[int, dict[str, Any]]:
        decisions: dict[int, dict[str, Any]] = {}
        remaining = list(batch)
        for _attempt in range(ROUTE_RESOLVER_OUTPUT_ATTEMPTS):
            cards = [
                route_resolution_card(
                    local_index, candidates[index], route_targets, context
                )
                for local_index, index in enumerate(remaining)
            ]
            try:
                parsed = complete_json(
                    route_resolution_prompt(cards),
                    provider=provider,
                    role="resolver",
                    llm_provider=llm_provider,
                    paths=paths,
                    max_attempts=1,
                    usage_cycle_id=usage_cycle_id,
                    usage_run_id=usage_run_id,
                    usage_stage=usage_stage,
                )
            except (LLMProviderError, ValueError, TypeError):
                continue
            batch_decisions = normalize_batch_decision_indexes(
                decisions_by_candidate_index(parsed), remaining
            )
            decisions.update(
                {
                    index: decision
                    for index, decision in batch_decisions.items()
                    if not route_decision_needs_retry(
                        candidates[index], decision, route_targets
                    )
                }
            )
            remaining = [index for index in remaining if index not in decisions]
            if not remaining:
                break
        for index in remaining:
            for _attempt in range(ROUTE_RESOLVER_SINGLE_OUTPUT_ATTEMPTS):
                card = route_resolution_card(
                    0, candidates[index], route_targets, context
                )
                try:
                    parsed = complete_json(
                        route_resolution_prompt([card]),
                        provider=provider,
                        role="resolver",
                        llm_provider=llm_provider,
                        paths=paths,
                        max_attempts=1,
                        usage_cycle_id=usage_cycle_id,
                        usage_run_id=usage_run_id,
                        usage_stage=usage_stage,
                    )
                except (LLMProviderError, ValueError, TypeError):
                    continue
                singleton = normalize_batch_decision_indexes(
                    decisions_by_candidate_index(parsed), [index]
                ).get(index)
                if singleton is None or route_decision_needs_retry(
                    candidates[index], singleton, route_targets
                ):
                    continue
                decisions[index] = singleton
                break
        return decisions

    if len(batches) <= 1 or llm_provider is not None:
        return {batch[0]: resolve(batch) for batch in batches}
    output: dict[int, dict[int, dict[str, Any]]] = {}
    with ThreadPoolExecutor(
        max_workers=min(ROUTE_RESOLVER_MAX_WORKERS, len(batches)),
        thread_name_prefix="route-resolver",
    ) as executor:
        futures = {executor.submit(resolve, batch): batch[0] for batch in batches}
        for future in as_completed(futures):
            output[futures[future]] = future.result()
    return output


def decision_needs_output_retry(decision: dict[str, Any]) -> bool:
    decision_name = str(decision.get("decision") or "")
    rationale = str(decision.get("rationale") or "").casefold()
    if (
        decision_name not in {"route_existing", "create_new_page", "needs_human"}
        or optional_float(decision.get("confidence")) is None
        or not rationale.strip()
        or (
            decision_name in {"route_existing", "create_new_page"}
            and not normalize_page_hint(decision.get("page_hint"))
        )
    ):
        return True
    if decision_name != "needs_human":
        return False
    missing_context = (
        "not present",
        "not provided",
        "not included",
        "unavailable",
        "missing",
        "truncated",
    )
    return any(term in rationale for term in missing_context) and (
        "card" in rationale
        or "candidate index" in rationale
        or "candidate details" in rationale
        or "candidate information" in rationale
        or "source fact" in rationale
        or "routing context" in rationale
        or "prompt" in rationale
    )


def route_decision_needs_retry(
    candidate: dict[str, Any],
    decision: dict[str, Any],
    route_targets: dict[str, dict[str, Any]],
) -> bool:
    if decision_needs_output_retry(decision):
        return True
    if str(decision.get("decision") or "") != "route_existing":
        return False
    page_hint = normalize_page_hint(decision.get("page_hint"))
    return page_hint in route_targets and not company_route_matches_mentions(
        candidate, page_hint
    )


def normalize_batch_decision_indexes(
    decisions: dict[int, dict[str, Any]], batch: list[int]
) -> dict[int, dict[str, Any]]:
    local = set(range(len(batch)))
    if set(decisions).issubset(local):
        return {batch[index]: decision for index, decision in decisions.items()}
    expected = set(batch)
    if set(decisions).issubset(expected):
        return decisions
    return {index: decision for index, decision in decisions.items() if index in expected}


def mark_unresolved_route_decision(
    candidate: dict[str, Any],
    decision: dict[str, Any] | None,
    *,
    resolution: str,
) -> dict[str, Any]:
    unresolved = deepcopy(candidate)
    metadata = dict(unresolved.get("metadata") or {})
    routing = dict(metadata.get("routing") or {})
    routing.update(
        {
            "route_resolution": resolution,
            "route_resolver_decision": (decision or {}).get("decision"),
            "route_resolver_proposed_page_hint": (decision or {}).get("page_hint"),
            "route_resolver_confidence": optional_float(
                (decision or {}).get("confidence")
            ),
            "route_resolver_rationale": str((decision or {}).get("rationale") or "")[:1000],
        }
    )
    metadata["routing"] = routing
    unresolved["metadata"] = metadata
    return unresolved


def candidate_requires_route_resolution(candidate: dict[str, Any]) -> bool:
    page_hint = normalize_page_hint(candidate.get("page_hint"))
    routing = candidate_route_metadata(candidate)
    if routing.get("event_temporal_identity_guard_locked") is True:
        return False
    return bool(
        not page_hint
        or page_hint in FALLBACK_PAGE_HINTS
        or page_hint.startswith("inbox/")
        or routing.get("route_destination_valid") is False
    )


def candidate_route_metadata(candidate: dict[str, Any]) -> dict[str, Any]:
    metadata = candidate.get("metadata")
    if not isinstance(metadata, dict):
        return {}
    routing = metadata.get("routing")
    return routing if isinstance(routing, dict) else {}


def fact_route_reclaim_query(candidate: dict[str, Any]) -> str:
    metadata = (
        candidate.get("metadata") if isinstance(candidate.get("metadata"), dict) else {}
    )
    mention_surfaces = [
        str(mention["surface"])
        for mention in (
            metadata.get("model_entity_mentions")
            or candidate.get("entity_mentions")
            or []
        )
        if isinstance(mention, dict) and mention.get("surface")
    ]
    return "\n".join(
        str(value)
        for value in (
            candidate.get("statement"),
            candidate.get("entity_key"),
            candidate.get("entity_mention"),
            metadata.get("model_entity_key"),
            " ".join(mention_surfaces),
            candidate.get("section_hint"),
            candidate.get("evidence_quote"),
        )
        if str(value or "").strip()
    )


def reclaim_route_record(
    question: Any,
    action: dict[str, Any],
    candidate: dict[str, Any],
    rerouted: dict[str, Any],
) -> dict[str, Any]:
    routing = candidate_route_metadata(rerouted)
    return {
        "question_id": question["id"],
        "old_action_id": str(question["action_id"] or ""),
        "old_evidence_json": action.get("evidence_json") or {},
        "candidate": rerouted,
        "old_page_hint": candidate.get("page_hint"),
        "new_page_hint": rerouted.get("page_hint"),
        "route_score": routing.get("reclaim_route_score")
        or routing.get("route_resolver_confidence"),
        "route_overlap": routing.get("reclaim_route_overlap") or [],
        "route_resolution": routing.get("route_resolution"),
        "route_rationale": routing.get("route_resolver_rationale"),
        "statement": str(rerouted.get("statement") or "")[:220],
    }


def load_route_resolution_context(
    paths: BrainPaths,
    candidates: list[dict[str, Any]],
    route_targets: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    document_ids = sorted(
        {
            fact_document_id(candidate)
            for candidate in candidates
            if fact_document_id(candidate)
        }
    )
    documents: dict[str, dict[str, Any]] = {}
    priors: dict[str, list[dict[str, Any]]] = {}
    siblings: dict[str, list[dict[str, Any]]] = {}
    with connection(paths.sqlite_path) as conn:
        for document_id in document_ids:
            row = conn.execute(
                """
                SELECT id, title, source_type, source_path, raw_path,
                       created_at, ingested_at
                FROM documents WHERE id = ?
                """,
                (document_id,),
            ).fetchone()
            if row is not None:
                document = dict(row)
                document.update(document_source_date_metadata(document))
                documents[document_id] = document
            priors[document_id] = load_document_route_priors(conn, document_id)
            siblings[document_id] = active_document_siblings(conn, document_id)
    current_siblings: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for candidate in candidates:
        document_id = fact_document_id(candidate)
        if document_id and not candidate_requires_route_resolution(candidate):
            current_siblings[document_id].append(
                {
                    "statement": candidate.get("statement"),
                    "page_hint": candidate.get("page_hint"),
                    "routing_confidence": candidate.get("routing_confidence"),
                }
            )
    return {
        "documents": documents,
        "priors": priors,
        "siblings": siblings,
        "current_siblings": current_siblings,
        "route_targets": route_targets,
    }


def active_document_siblings(conn: Any, document_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT DISTINCT f.id, f.statement, f.page_hint, f.section_hint,
                        f.routing_confidence
        FROM facts f
        WHERE f.status = 'active'
          AND (
            json_extract(
              CASE WHEN json_valid(f.metadata) THEN f.metadata ELSE '{}'
              END,
              '$.document_id'
            ) = ?
            OR EXISTS (
              SELECT 1
              FROM json_each(
                CASE WHEN json_valid(f.source_ids) THEN f.source_ids ELSE '[]' END
              ) source
              JOIN chunks c ON source.value = ('chunk:' || c.id)
              WHERE c.document_id = ?
            )
          )
        ORDER BY COALESCE(f.routing_confidence, 0) DESC, f.id
        LIMIT ?
        """,
        (document_id, document_id, ROUTE_RESOLVER_SIBLING_LIMIT),
    )
    return [dict(row) for row in rows]


def route_resolution_card(
    index: int,
    candidate: dict[str, Any],
    route_targets: dict[str, dict[str, Any]],
    context: dict[str, Any],
) -> dict[str, Any]:
    document_id = fact_document_id(candidate)
    document = context["documents"].get(document_id) or {}
    siblings = [
        *context["siblings"].get(document_id, []),
        *context["current_siblings"].get(document_id, []),
    ]
    return {
        "candidate_index": index,
        "source_document": {
            "document_id": document_id,
            "title": document.get("title"),
            "source_type": document.get("source_type"),
            "source_date": document.get("source_date"),
        },
        "fact": {
            "statement": candidate.get("statement"),
            "evidence_quote": candidate.get("evidence_quote"),
            "entity_mention": candidate.get("entity_mention"),
            "entity_type": candidate.get("entity_type"),
            "section_hint": candidate.get("section_hint"),
            "current_page_hint": candidate.get("page_hint"),
        },
        "deterministic_route_suggestion": candidate_route_metadata(candidate).get(
            "deterministic_route_suggestion"
        ),
        "same_source_routes": same_source_route_cards(
            context["priors"].get(document_id, []), siblings, route_targets
        ),
        "existing_route_candidates": ranked_route_target_cards(
            candidate,
            document,
            route_targets,
            context["priors"].get(document_id, []),
        ),
    }


def same_source_route_cards(
    priors: list[dict[str, Any]],
    siblings: list[dict[str, Any]],
    route_targets: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    examples: dict[str, list[str]] = defaultdict(list)
    for sibling in siblings:
        page_hint = normalize_page_hint(sibling.get("page_hint"))
        statement = str(sibling.get("statement") or "").strip()
        if page_hint and statement and statement not in examples[page_hint]:
            examples[page_hint].append(statement[:240])
    cards = []
    seen = set()
    for prior in priors:
        page_hint = normalize_page_hint(prior.get("page_hint"))
        if not page_hint or page_hint in seen:
            continue
        seen.add(page_hint)
        cards.append(
            {
                "page_hint": page_hint,
                "target_exists": page_hint in route_targets,
                "fact_count": int(prior.get("fact_count") or 0),
                "share": float(prior.get("share") or 0.0),
                "example_facts": examples.get(page_hint, [])[:3],
            }
        )
    for page_hint, statements in examples.items():
        if page_hint not in seen:
            cards.append(
                {
                    "page_hint": page_hint,
                    "target_exists": page_hint in route_targets,
                    "fact_count": len(statements),
                    "share": 0.0,
                    "example_facts": statements[:3],
                }
            )
    return cards[:ROUTE_RESOLVER_CANDIDATE_LIMIT]


def ranked_route_target_cards(
    candidate: dict[str, Any],
    document: dict[str, Any],
    route_targets: dict[str, dict[str, Any]],
    priors: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    query = "\n".join(
        [fact_route_reclaim_query(candidate), str(document.get("title") or "")]
    )
    query_tokens = route_tokens(query)
    prior_counts = {
        normalize_page_hint(prior.get("page_hint")): int(prior.get("fact_count") or 0)
        for prior in priors
    }
    ranked = []
    for page_hint, target in route_targets.items():
        target_text = " ".join(
            str(target.get(key) or "")
            for key in (
                "page_hint",
                "canonical_entity",
                "page_scope",
                "retrieval_purpose",
            )
        )
        overlap = sorted(query_tokens & route_tokens(target_text))
        sibling_count = prior_counts.get(normalize_page_hint(page_hint), 0)
        score = (len(overlap) * 5) + min(8, sibling_count)
        if not score:
            continue
        ranked.append(
            (
                score,
                sibling_count,
                page_hint,
                {
                    "page_hint": page_hint,
                    "title": target.get("canonical_entity"),
                    "page_scope": target.get("page_scope"),
                    "retrieval_purpose": target.get("retrieval_purpose"),
                    "same_source_fact_count": sibling_count,
                    "matching_terms": overlap[:12],
                },
            )
        )
    ranked.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)
    return [item[3] for item in ranked[:ROUTE_RESOLVER_CANDIDATE_LIMIT]]


def route_resolution_prompt(cards: list[dict[str, Any]]) -> str:
    candidate_indexes = [card["candidate_index"] for card in cards]
    return (
        "You are the routing resolver for PKM Brain. Resolve only facts that the extractor could "
        "not route cleanly. Return exactly one decision for every candidate_index in this batch: "
        f"{candidate_indexes}. Do not omit or renumber indexes.\n\n"
        "Choose route_existing when an active canonical page is semantically plausible. Facts from "
        "one source document usually belong with other facts from that source; treat coherent sibling "
        "routes as a strong prior unless this fact clearly changes topic. A plausible same-source route "
        "is preferable to human review even when its wording is not an exact lexical match.\n\n"
        "A deterministic_route_suggestion is only a lexical/coherence hint. Reject it when the source "
        "topic or fact meaning does not fit; generic token overlap is not routing evidence.\n\n"
        "If a fact names an organization, never route it to a different organization's company page. "
        "Use a coherent topical/person page or create the named organization's canonical page.\n\n"
        "Use route_existing only for a destination whose target_exists value is true or that appears "
        "in existing_route_candidates. If a coherent same-source destination is not materialized yet, "
        "use create_new_page instead of calling it an existing route.\n\n"
        "Choose create_new_page only when the source clearly establishes a durable named topic that no "
        "existing candidate represents. Use a concise wiki-relative .md path under career, companies, "
        "concepts, decisions, events, ideas, open_loops, people, products, or projects. Never use inbox, "
        "references, raw, logs, or concepts/extracted-facts.md.\n\n"
        "Choose needs_human only when two materially different destinations remain equally plausible, "
        "or the source lacks enough identity/topic context to name a safe destination. Do not use human "
        "review merely because routing is imperfect. Confidence is confidence in the routing decision, "
        "not confidence in the fact's truth.\n\n"
        "Return this exact object shape, with one decision object per requested index: "
        '{"decisions":[{"candidate_index":0,"decision":"route_existing",'
        '"page_hint":"concepts/example.md","confidence":0.9,'
        '"rationale":"Why this destination fits."}]}\n\n'
        f"Routing cards JSON:\n{json.dumps(cards, ensure_ascii=False, indent=2)}"
    )


def decisions_by_candidate_index(parsed: dict[str, Any]) -> dict[int, dict[str, Any]]:
    raw_decisions = parsed.get("decisions")
    if not isinstance(raw_decisions, list):
        properties = parsed.get("properties")
        decision_property = (
            properties.get("decisions") if isinstance(properties, dict) else None
        )
        raw_decisions = (
            decision_property.get("items")
            if isinstance(decision_property, dict)
            else []
        )
    output = {}
    for item in raw_decisions or []:
        if not isinstance(item, dict):
            continue
        try:
            index = int(item.get("candidate_index"))
        except (TypeError, ValueError):
            continue
        output[index] = item
    return output


def apply_route_resolution_decision(
    candidate: dict[str, Any],
    decision: dict[str, Any],
    route_targets: dict[str, dict[str, Any]],
    *,
    min_confidence: float,
) -> dict[str, Any] | None:
    decision_name = str(decision.get("decision") or "").strip()
    confidence = optional_float(decision.get("confidence"))
    if (
        decision_name not in {"route_existing", "create_new_page"}
        or confidence is None
        or confidence < min_confidence
    ):
        return None
    page_hint = normalize_page_hint(decision.get("page_hint"))
    if decision_name == "route_existing":
        if page_hint not in route_targets:
            page_hint = closest_existing_page(page_hint, route_targets)
            if not page_hint:
                return None
        target_exists = True
        resolution = (
            "resolver_existing_page"
            if normalize_page_hint(decision.get("page_hint")) == page_hint
            else "resolver_fuzzy_snapped_existing_page"
        )
    else:
        page_hint = canonical_new_identity_page(candidate, page_hint, route_targets)
        page_hint, target_exists = snap_or_validate_new_page(page_hint, route_targets)
        if not page_hint:
            return None
        resolution = (
            "resolver_fuzzy_snapped_existing_page"
            if target_exists
            else "resolver_new_canonical_page"
        )
    routed = deepcopy(candidate)
    original_page_hint = normalize_page_hint(routed.get("page_hint"))
    routed["page_hint"] = page_hint
    routed["entity_key"] = entity_key_for_change(
        topic_for_path(page_hint), page_hint, str(routed.get("section_hint") or "Summary")
    )
    routed["routing_confidence"] = confidence
    metadata = (
        dict(routed.get("metadata"))
        if isinstance(routed.get("metadata"), dict)
        else {}
    )
    metadata["routing"] = {
        **candidate_route_metadata(routed),
        "original_page_hint": original_page_hint,
        "normalized_page_hint": page_hint,
        "route_destination_valid": True,
        "route_target_exists": target_exists,
        "route_resolution": resolution,
        "route_resolver_decision": decision_name,
        "route_resolver_confidence": confidence,
        "route_resolver_rationale": str(decision.get("rationale") or "")[:1000],
    }
    routed["metadata"] = metadata
    return routed


def snap_or_validate_new_page(
    page_hint: str, route_targets: dict[str, dict[str, Any]]
) -> tuple[str, bool]:
    if page_hint in route_targets:
        return page_hint, True
    closest = closest_existing_page(page_hint, route_targets)
    if closest:
        return closest, True
    return (page_hint, False) if valid_new_page_hint(page_hint) else ("", False)


def canonical_new_identity_page(
    candidate: dict[str, Any],
    page_hint: str,
    route_targets: dict[str, dict[str, Any]],
) -> str:
    if page_hint in route_targets:
        return page_hint
    namespace = page_hint.split("/", 1)[0] if "/" in page_hint else ""
    entity_type = {"companies": "organization", "people": "person"}.get(namespace)
    if entity_type is None:
        return page_hint
    stem = page_hint.removeprefix(f"{namespace}/").removesuffix(".md")
    for surface in named_entity_surfaces(candidate, entity_type):
        identity_slug = slugify(surface)
        if not identity_slug or not (
            stem == identity_slug or stem.startswith(f"{identity_slug}-")
        ):
            continue
        canonical = f"{namespace}/{identity_slug}.md"
        if canonical in route_targets:
            return canonical
        if namespace == "companies" and any(
            target.startswith(f"companies/{identity_slug}/")
            for target in route_targets
        ):
            return page_hint
        return canonical
    return page_hint


def company_route_matches_mentions(
    candidate: dict[str, Any], page_hint: str
) -> bool:
    if not page_hint.startswith("companies/"):
        return True
    company_path = page_hint.removeprefix("companies/").removesuffix(".md")
    organization_slugs = {
        slugify(surface) for surface in named_entity_surfaces(candidate, "organization")
    }
    organization_slugs.discard("")
    if not organization_slugs:
        return True
    return any(
        company_path == identity
        or company_path.startswith(f"{identity}-")
        or company_path.startswith(f"{identity}/")
        for identity in organization_slugs
    )


def named_entity_surfaces(candidate: dict[str, Any], entity_type: str) -> list[str]:
    metadata = candidate.get("metadata")
    if isinstance(metadata, str):
        metadata = loads(metadata, {})
    metadata = metadata if isinstance(metadata, dict) else {}
    mentions = metadata.get("model_entity_mentions") or candidate.get(
        "entity_mentions"
    )
    return [
        str(mention.get("surface") or "").strip()
        for mention in mentions or []
        if isinstance(mention, dict)
        and str(mention.get("entity_type") or "") == entity_type
        and str(mention.get("mention_kind") or "named") == "named"
        and str(mention.get("surface") or "").strip()
    ]


def closest_existing_page(
    page_hint: str, route_targets: dict[str, dict[str, Any]]
) -> str:
    namespace = page_hint.split("/", 1)[0] if "/" in page_hint else ""
    if namespace == "people":
        proposed_stem = page_hint.removeprefix("people/").removesuffix(".md")
        person_matches = [
            existing
            for existing in route_targets
            if existing.startswith("people/")
            and proposed_stem.startswith(
                existing.removeprefix("people/").removesuffix(".md") + "-"
            )
        ]
        if person_matches:
            return max(person_matches, key=len)
    best = ""
    best_score = 0.0
    for existing in route_targets:
        existing_namespace = existing.split("/", 1)[0] if "/" in existing else ""
        if namespace and existing_namespace != namespace:
            continue
        score = SequenceMatcher(None, page_hint, existing).ratio()
        if score > best_score:
            best = existing
            best_score = score
    return best if best_score >= ROUTE_FUZZY_DUP_THRESHOLD else ""


def valid_new_page_hint(page_hint: str) -> bool:
    if (
        not page_hint
        or page_hint in FALLBACK_PAGE_HINTS
        or page_hint.startswith("/")
        or ".." in page_hint.split("/")
        or "://" in page_hint
        or not page_hint.endswith(".md")
        or any(page_hint.startswith(prefix) for prefix in INVALID_ROUTE_PREFIXES)
    ):
        return False
    namespace = page_hint.split("/", 1)[0] if "/" in page_hint else ""
    return namespace in CANONICAL_ROUTE_NAMESPACES


def normalize_page_hint(value: Any) -> str:
    normalized = canonical_page_hint_for_fact(str(value or "").strip())
    while normalized.startswith("wiki/"):
        normalized = canonical_page_hint_for_fact(normalized.removeprefix("wiki/"))
    return normalized


def fact_document_id(fact: dict[str, Any]) -> str:
    metadata = fact.get("metadata")
    if isinstance(metadata, str):
        metadata = loads(metadata, {})
    return str(metadata.get("document_id") or "").strip() if isinstance(metadata, dict) else ""


def route_tokens(value: str) -> set[str]:
    return {
        token
        for raw in ROUTE_TOKEN_RE.findall(value.casefold().replace("-", " "))
        for token in re.split(r"[_\s]+", raw)
        if len(token) >= 3 and token not in ROUTE_STOP_TOKENS
    }


def optional_float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def batched(values: list[int], size: int) -> list[list[int]]:
    return [values[index : index + size] for index in range(0, len(values), size)]
