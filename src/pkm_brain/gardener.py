from __future__ import annotations

import re
from collections import Counter
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from .contracts import active_page_contracts, validate_fact_against_contract
from .cos_actions import propose_action
from .db import connection, loads
from .llm import LLMProvider, complete_json, cos_role_provider_configured
from .paths import BrainPaths
from .wiki_facts import managed_fact_page_summaries


MERGE_PATH_SIMILARITY_FLOOR = 0.86
MERGE_EVIDENCE_OVERLAP_FLOOR = 0.25
MERGE_EVIDENCE_ONLY_PATH_FLOOR = 0.60
MERGE_STRONG_EVIDENCE_OVERLAP_FLOOR = 0.45
PAGE_SPLIT_FACT_FLOOR = 5
PAGE_SPLIT_SECTION_FLOOR = 3
REHOME_DESTINATION_SCORE_FLOOR = 0.35
SUPPRESSED_GARDENER_STATUSES = ("failed", "reverted", "rejected", "dismissed")
TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9_/-]{2,}")
STOP_TOKENS = {
    "about",
    "active",
    "concept",
    "concepts",
    "document",
    "documents",
    "fact",
    "facts",
    "loops",
    "managed",
    "markdown",
    "note",
    "notes",
    "open",
    "overview",
    "page",
    "pages",
    "project",
    "projects",
    "reference",
    "references",
    "section",
    "sections",
    "source",
    "sources",
    "status",
    "summary",
    "wiki",
}
GARDENER_JUDGMENT_SCHEMA = {
    "type": "object",
    "required": ["judgments"],
    "properties": {
        "judgments": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["candidate_key", "decision", "rationale"],
            },
        }
    },
}


def generate_gardener_candidates(
    paths: BrainPaths,
    *,
    shadow: bool = True,
    max_candidates: int = 25,
    llm_provider: LLMProvider | None = None,
    provider: str | None = None,
) -> dict[str, Any]:
    pages = enrich_gardener_pages(paths, managed_fact_page_summaries(paths))
    contracts = {contract["page_hint"]: contract for contract in active_page_contracts(paths)}
    suppressed_keys = recent_suppressed_candidate_keys(paths)
    candidates = deterministic_topology_candidates(
        pages, contracts, suppressed_candidate_keys=suppressed_keys
    )[:max_candidates]
    llm_result = apply_gardener_judgment(
        candidates,
        pages,
        contracts,
        paths=paths,
        llm_provider=llm_provider,
        provider=provider,
    )
    candidates = llm_result["candidates"][:max_candidates]
    actions: list[dict[str, Any]] = []
    if not shadow:
        for candidate in candidates:
            actions.append(propose_gardener_action(paths, candidate))
    return {
        "status": "ok",
        "shadow": shadow,
        "candidate_count": len(candidates),
        "suppressed_candidate_count": len(suppressed_keys),
        "llm_judgment": llm_result["summary"],
        "candidates": candidates,
        "actions": actions,
    }


def deterministic_topology_candidates(
    pages: list[dict[str, Any]],
    contracts: dict[str, dict[str, Any]],
    *,
    suppressed_candidate_keys: set[str] | None = None,
) -> list[dict[str, Any]]:
    suppressed_candidate_keys = suppressed_candidate_keys or set()
    candidates: list[dict[str, Any]] = []
    for index, left in enumerate(pages):
        for right in pages[index + 1 :]:
            append_candidate(
                candidates,
                merge_candidate(left, right, contracts),
                suppressed_candidate_keys,
            )
    for page in pages:
        active_count = int(page.get("active_fact_count") or 0)
        has_contract = page["relative_path"] in contracts
        if active_count == 1 and not has_contract:
            append_candidate(
                candidates,
                rehome_candidate(page, pages, contracts),
                suppressed_candidate_keys,
            )
        if active_count >= PAGE_SPLIT_FACT_FLOOR:
            append_candidate(
                candidates,
                page_split_candidate(page, contracts),
                suppressed_candidate_keys,
            )
        if active_count >= 2 and has_contract:
            append_candidate(
                candidates,
                contract_mismatch_candidate(page, contracts[page["relative_path"]]),
                suppressed_candidate_keys,
            )
        if active_count >= 2 and not has_contract:
            append_candidate(
                candidates,
                missing_contract_candidate(page),
                suppressed_candidate_keys,
            )
    candidates.sort(key=candidate_sort_key)
    return candidates


def propose_gardener_action(paths: BrainPaths, candidate: dict[str, Any]) -> dict[str, Any]:
    action_type = str(candidate["action_type"])
    contract = candidate.get("contract") if isinstance(candidate.get("contract"), dict) else None
    target_fact_ids = [str(candidate["fact_id"])] if candidate.get("fact_id") else []
    target_contract_ids = [str(contract["id"])] if contract and contract.get("id") else []
    return propose_action(
        paths,
        action_type,
        action_payload=gardener_action_payload(candidate),
        evidence={
            "gardener_judgment": candidate.get("llm_judgment"),
        }
        if candidate.get("llm_judgment")
        else None,
        action_features={
            "candidate_key": candidate.get("candidate_key"),
            "candidate_signal": candidate.get("reason"),
            "score": candidate.get("score"),
            "similarity": candidate.get("similarity"),
            "affected_page_count": len(candidate.get("page_hints") or []),
            "reversible": True,
            "eval_gate": {"suite": "topology", "passed": False},
        },
        target_fact_ids=target_fact_ids,
        target_page_paths=[str(path) for path in candidate.get("page_hints") or []],
        target_contract_ids=target_contract_ids,
        proposed_by="gardener_llm" if candidate.get("llm_judgment") else "gardener",
        confidence=float(candidate.get("score") or candidate.get("similarity") or 0.0),
        risk_tier=str(candidate.get("risk_tier") or "medium"),
    )


def page_similarity(left: str, right: str) -> float:
    left_key = Path(left).with_suffix("").as_posix()
    right_key = Path(right).with_suffix("").as_posix()
    return SequenceMatcher(None, left_key, right_key).ratio()


def apply_gardener_judgment(
    candidates: list[dict[str, Any]],
    pages: list[dict[str, Any]],
    contracts: dict[str, dict[str, Any]],
    *,
    paths: BrainPaths | None,
    llm_provider: LLMProvider | None,
    provider: str | None,
) -> dict[str, Any]:
    role_configured = (
        llm_provider is not None
        or bool((provider or "").strip())
        or (paths is not None and cos_role_provider_configured(paths, "gardener"))
    )
    if not candidates or not role_configured:
        return {
            "candidates": candidates,
            "summary": {
                "enabled": False,
                "judgment_count": 0,
                "dropped_candidate_count": 0,
                "reason": None if not candidates else "No CoS LLM provider configured for gardener role",
            },
        }
    parsed = complete_json(
        gardener_judgment_prompt(candidates, pages, contracts),
        schema=GARDENER_JUDGMENT_SCHEMA,
        provider=provider,
        role="gardener",
        llm_provider=llm_provider,
        paths=paths,
    )
    judgments = {
        str(item.get("candidate_key") or ""): item
        for item in parsed.get("judgments") or []
        if isinstance(item, dict) and item.get("candidate_key")
    }
    kept: list[dict[str, Any]] = []
    dropped = 0
    for candidate in candidates:
        key = str(candidate.get("candidate_key") or "")
        judgment = normalize_gardener_judgment(judgments.get(key))
        if judgment is None:
            kept.append(candidate)
            continue
        if judgment["decision"] == "drop":
            dropped += 1
            continue
        row = dict(candidate)
        row["llm_judgment"] = judgment
        if judgment.get("risk_tier") in {"low", "medium", "high"}:
            row["risk_tier"] = judgment["risk_tier"]
        if judgment.get("score_adjustment") is not None:
            row["score"] = round(
                min(1.0, max(0.0, float(row.get("score") or 0.0) + float(judgment["score_adjustment"]))),
                4,
            )
        kept.append(row)
    kept.sort(key=candidate_sort_key)
    return {
        "candidates": kept,
        "summary": {
            "enabled": True,
            "judgment_count": len(judgments),
            "dropped_candidate_count": dropped,
        },
    }


def normalize_gardener_judgment(judgment: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(judgment, dict):
        return None
    decision = str(judgment.get("decision") or "keep").strip().lower()
    if decision not in {"keep", "drop"}:
        decision = "keep"
    output = {
        "candidate_key": str(judgment.get("candidate_key") or ""),
        "decision": decision,
        "rationale": str(judgment.get("rationale") or "").strip()[:1000],
    }
    if judgment.get("score_adjustment") is not None:
        try:
            output["score_adjustment"] = float(judgment["score_adjustment"])
        except (TypeError, ValueError):
            pass
    risk_tier = str(judgment.get("risk_tier") or "").strip().lower()
    if risk_tier in {"low", "medium", "high"}:
        output["risk_tier"] = risk_tier
    return output


def gardener_judgment_prompt(
    candidates: list[dict[str, Any]],
    pages: list[dict[str, Any]],
    contracts: dict[str, dict[str, Any]],
) -> str:
    candidate_cards = [
        {
            "candidate_key": candidate.get("candidate_key"),
            "action_type": candidate.get("action_type"),
            "page_hints": candidate.get("page_hints"),
            "reason": candidate.get("reason"),
            "score": candidate.get("score"),
            "similarity": candidate.get("similarity"),
            "evidence_overlap": candidate.get("evidence_overlap"),
            "contract_check": candidate.get("contract_check"),
            "contract_validation": candidate.get("contract_validation"),
            "contract_violations": candidate.get("contract_violations"),
        }
        for candidate in candidates
    ]
    page_cards = [
        {
            "relative_path": page.get("relative_path"),
            "title": page.get("title"),
            "active_fact_count": page.get("active_fact_count"),
            "entity_keys": page.get("entity_keys"),
            "section_counts": page.get("section_counts"),
            "fact_statements": list(page.get("fact_statements") or [])[:5],
        }
        for page in pages
        if page.get("relative_path") in {
            page_hint
            for candidate in candidates
            for page_hint in candidate.get("page_hints") or []
        }
    ]
    contract_cards = [
        {
            "page_hint": page_hint,
            "canonical_entity": contract.get("canonical_entity"),
            "page_scope": contract.get("page_scope"),
            "what_belongs_here": contract.get("what_belongs_here"),
            "what_does_not_belong_here": contract.get("what_does_not_belong_here"),
        }
        for page_hint, contract in sorted(contracts.items())
        if page_hint in {
            page_hint
            for candidate in candidates
            for page_hint in candidate.get("page_hints") or []
        }
    ]
    return (
        "Review deterministic PKM Brain gardener candidates. "
        "You may only keep or drop existing candidate_key values; do not invent new candidates, page_hints, or fact_ids. "
        "Drop candidates that violate contracts, merge unrelated scopes, or lack enough evidence. "
        "Return JSON judgments with candidate_key, decision keep/drop, rationale, optional score_adjustment, optional risk_tier.\n\n"
        f"Candidates:\n{candidate_cards}\n\nPages:\n{page_cards}\n\nContracts:\n{contract_cards}"
    )


def enrich_gardener_pages(
    paths: BrainPaths, pages: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    page_hints = [str(page.get("relative_path") or "") for page in pages]
    evidence_by_page = load_page_fact_evidence(paths, page_hints)
    enriched: list[dict[str, Any]] = []
    for page in pages:
        page_hint = str(page.get("relative_path") or "")
        evidence = evidence_by_page.get(page_hint, {})
        row = dict(page)
        row["fact_ids"] = list(evidence.get("fact_ids") or [])
        row["facts"] = list(evidence.get("facts") or [])
        row["fact_statements"] = list(evidence.get("fact_statements") or [])
        row["entity_keys"] = sorted(evidence.get("entity_keys") or [])
        row["source_ids"] = sorted(evidence.get("source_ids") or [])
        row["section_counts"] = dict(evidence.get("section_counts") or {})
        row["fact_tokens"] = sorted(evidence.get("fact_tokens") or [])
        row["page_tokens"] = sorted(
            tokenize_signal(
                page_hint,
                row.get("title"),
                row.get("page_type"),
                row.get("status"),
            )
        )
        enriched.append(row)
    return enriched


def load_page_fact_evidence(
    paths: BrainPaths, page_hints: list[str]
) -> dict[str, dict[str, Any]]:
    page_hints = sorted({page_hint for page_hint in page_hints if page_hint})
    if not page_hints:
        return {}
    evidence: dict[str, dict[str, Any]] = {}
    placeholders = ",".join("?" for _ in page_hints)
    with connection(paths.sqlite_path) as conn:
        for row in conn.execute(
            f"""
            SELECT id, statement, entity_key, page_hint, section_hint, source_ids
            FROM facts
            WHERE status = 'active'
              AND page_hint IN ({placeholders})
            ORDER BY page_hint, observed_at DESC, created_at DESC, id
            """,
            page_hints,
        ):
            page_hint = str(row["page_hint"] or "")
            bucket = evidence.setdefault(
                page_hint,
                {
                    "fact_ids": [],
                    "facts": [],
                    "fact_statements": [],
                    "entity_keys": set(),
                    "source_ids": set(),
                    "section_counts": Counter(),
                    "fact_tokens": set(),
                },
            )
            source_ids = loads(row["source_ids"], [])
            if not isinstance(source_ids, list):
                source_ids = []
            section_hint = str(row["section_hint"] or "Unsectioned")
            fact = {
                "id": str(row["id"]),
                "statement": str(row["statement"] or ""),
                "entity_key": str(row["entity_key"] or ""),
                "page_hint": page_hint,
                "section_hint": section_hint,
                "source_ids": [str(source_id) for source_id in source_ids if source_id],
            }
            bucket["fact_ids"].append(str(row["id"]))
            bucket["facts"].append(fact)
            bucket["fact_statements"].append(str(row["statement"] or ""))
            bucket["entity_keys"].add(str(row["entity_key"] or ""))
            bucket["source_ids"].update(str(source_id) for source_id in source_ids if source_id)
            bucket["section_counts"][section_hint] += 1
            bucket["fact_tokens"].update(
                tokenize_signal(
                    row["statement"],
                    row["entity_key"],
                    section_hint,
                    source_ids,
                )
            )
    return evidence


def merge_candidate(
    left: dict[str, Any],
    right: dict[str, Any],
    contracts: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    left_hint = str(left["relative_path"])
    right_hint = str(right["relative_path"])
    contract_check = merge_contract_check(left, right, contracts)
    if not contract_check["compatible"]:
        return None
    path_similarity = page_similarity(left_hint, right_hint)
    fact_overlap = token_containment(
        set(left.get("fact_tokens") or []), set(right.get("fact_tokens") or [])
    )
    shared_entities = sorted(set(left.get("entity_keys") or []) & set(right.get("entity_keys") or []))
    shared_sources = sorted(set(left.get("source_ids") or []) & set(right.get("source_ids") or []))
    evidence_overlap = max(
        fact_overlap,
        0.45 if shared_entities else 0.0,
        0.35 if shared_sources else 0.0,
    )
    path_and_evidence = (
        path_similarity >= MERGE_PATH_SIMILARITY_FLOOR
        and evidence_overlap >= MERGE_EVIDENCE_OVERLAP_FLOOR
    )
    strong_evidence = (
        path_similarity >= MERGE_EVIDENCE_ONLY_PATH_FLOOR
        and evidence_overlap >= MERGE_STRONG_EVIDENCE_OVERLAP_FLOOR
    )
    if not (path_and_evidence or strong_evidence):
        return None
    page_hints = sorted([left_hint, right_hint])
    score = round(min(1.0, max(path_similarity, 0.55 + (0.4 * evidence_overlap))), 4)
    return {
        "action_type": "page_merge",
        "page_hints": page_hints,
        "candidate_key": candidate_key("page_merge", page_hints),
        "score": score,
        "similarity": round(path_similarity, 4),
        "fact_token_overlap": round(fact_overlap, 4),
        "evidence_overlap": round(evidence_overlap, 4),
        "shared_entity_keys": shared_entities[:5],
        "shared_source_count": len(shared_sources),
        "contract_check": contract_check,
        "reason": "near-duplicate page hints with overlapping fact evidence",
        "contracts_present": [
            left_hint in contracts,
            right_hint in contracts,
        ],
        "risk_tier": "medium",
    }


def rehome_candidate(
    page: dict[str, Any],
    pages: list[dict[str, Any]],
    contracts: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    source_fact = first_page_fact(page)
    if source_fact is None:
        return None
    destination, score, contract_validation = best_rehome_destination(
        page, pages, contracts, source_fact
    )
    if destination is None or score < REHOME_DESTINATION_SCORE_FLOOR:
        return None
    source_hint = str(page["relative_path"])
    destination_hint = str(destination["relative_path"])
    page_hints = [source_hint, destination_hint]
    return {
        "action_type": "rehome_fact",
        "page_hints": page_hints,
        "source_page_hint": source_hint,
        "destination_page_hint": destination_hint,
        "fact_id": source_fact["id"],
        "candidate_key": candidate_key("rehome_fact", page_hints, [source_fact["id"]]),
        "score": round(score, 4),
        "similarity": round(score, 4),
        "reason": "singleton page without contract overlaps a destination page",
        "contracts_present": [False, destination_hint in contracts],
        "contract_validation": contract_validation,
        "risk_tier": "low",
    }


def best_rehome_destination(
    page: dict[str, Any],
    pages: list[dict[str, Any]],
    contracts: dict[str, dict[str, Any]],
    source_fact: dict[str, Any],
) -> tuple[dict[str, Any] | None, float, dict[str, Any] | None]:
    source_tokens = set(page.get("fact_tokens") or []) | set(page.get("page_tokens") or [])
    source_entities = set(page.get("entity_keys") or [])
    source_sources = set(page.get("source_ids") or [])
    best_page: dict[str, Any] | None = None
    best_score = 0.0
    best_validation: dict[str, Any] | None = None
    for candidate in pages:
        if candidate["relative_path"] == page["relative_path"]:
            continue
        candidate_tokens = set(candidate.get("fact_tokens") or []) | set(candidate.get("page_tokens") or [])
        candidate_entities = set(candidate.get("entity_keys") or [])
        candidate_sources = set(candidate.get("source_ids") or [])
        active_count = int(candidate.get("active_fact_count") or 0)
        if active_count < 2 and candidate["relative_path"] not in contracts:
            continue
        contract = contracts.get(candidate["relative_path"])
        validation = None
        if contract is not None:
            validation = validate_fact_for_page(source_fact, str(candidate["relative_path"]), contract)
            if not validation.get("valid"):
                continue
        token_score = token_containment(source_tokens, candidate_tokens)
        entity_score = 0.45 if source_entities & candidate_entities else 0.0
        source_score = 0.35 if source_sources & candidate_sources else 0.0
        contract_score = float(validation.get("contract_scope_score") or 0.0) if validation else 0.0
        contract_bonus = 0.1 if candidate["relative_path"] in contracts else 0.0
        score = min(1.0, max(token_score, entity_score, source_score, contract_score) + contract_bonus)
        if score > best_score:
            best_page = candidate
            best_score = score
            best_validation = validation
    return best_page, best_score, best_validation


def page_split_candidate(
    page: dict[str, Any], contracts: dict[str, dict[str, Any]]
) -> dict[str, Any] | None:
    page_hint = str(page["relative_path"])
    section_counts = {
        str(section): int(count)
        for section, count in (page.get("section_counts") or {}).items()
        if section and str(section).lower() not in {"summary", "unsectioned"}
    }
    if len(section_counts) < PAGE_SPLIT_SECTION_FLOOR:
        return None
    active_count = int(page.get("active_fact_count") or 0)
    score = round(min(0.95, 0.45 + (active_count / 25.0) + (len(section_counts) * 0.05)), 4)
    return {
        "action_type": "page_split",
        "page_hints": [page_hint],
        "candidate_key": candidate_key("page_split", [page_hint]),
        "score": score,
        "similarity": 0.0,
        "reason": "dense page has active facts across multiple sections",
        "section_counts": dict(sorted(section_counts.items())),
        "contracts_present": [page_hint in contracts],
        "risk_tier": "medium",
    }


def missing_contract_candidate(page: dict[str, Any]) -> dict[str, Any]:
    page_hint = str(page["relative_path"])
    active_count = int(page.get("active_fact_count") or 0)
    score = round(min(0.85, 0.35 + (active_count / 20.0)), 4)
    contract = proposed_contract_for_page(page)
    return {
        "action_type": "edit_contract",
        "page_hints": [page_hint],
        "candidate_key": candidate_key("edit_contract", [page_hint]),
        "contract": contract,
        "score": score,
        "similarity": 0.0,
        "reason": "active managed fact page has no page contract",
        "contracts_present": [False],
        "risk_tier": "low",
    }


def contract_mismatch_candidate(
    page: dict[str, Any], contract: dict[str, Any]
) -> dict[str, Any] | None:
    page_hint = str(page["relative_path"])
    validations = [
        validate_fact_against_contract(fact, contract)
        for fact in page.get("facts") or []
        if isinstance(fact, dict)
    ]
    invalid = [validation for validation in validations if not validation.get("valid")]
    if not invalid:
        return None
    revised_contract = proposed_contract_revision_for_page(page, contract)
    score = round(min(0.9, 0.45 + (len(invalid) / max(1, len(validations))) * 0.35), 4)
    return {
        "action_type": "edit_contract",
        "page_hints": [page_hint],
        "candidate_key": candidate_key("edit_contract", [page_hint], [str(item) for item in page.get("fact_ids") or []]),
        "contract": revised_contract,
        "score": score,
        "similarity": 0.0,
        "reason": "active facts do not conform to the current page contract",
        "contract_violations": invalid[:5],
        "contracts_present": [True],
        "risk_tier": "low",
    }


def proposed_contract_for_page(page: dict[str, Any]) -> dict[str, Any]:
    page_hint = str(page.get("relative_path") or "")
    title = str(page.get("title") or human_title_for_candidate(page_hint))
    return {
        "id": f"contract_gardener_{stable_page_slug(page_hint)}",
        "page_hint": page_hint,
        "canonical_entity": title,
        "page_scope": f"Managed source-backed facts about {title}.",
        "retrieval_purpose": f"Answer factual questions about {title} using active facts and source evidence.",
        "what_belongs_here": "Atomic active facts routed to this page by entity and section.",
        "what_does_not_belong_here": "Uncited synthesis, stale draft text, and facts better governed by another page contract.",
        "freshness_policy": "Refresh when active facts change; unresolved truth conflicts stay visible as residue.",
        "related_pages": [],
        "version": 1,
        "status": "active",
    }


def proposed_contract_revision_for_page(
    page: dict[str, Any], contract: dict[str, Any]
) -> dict[str, Any]:
    proposed = proposed_contract_for_page(page)
    proposed["id"] = str(contract.get("id") or proposed["id"])
    proposed["version"] = int(contract.get("version") or 1) + 1
    proposed["page_scope"] = (
        contract.get("page_scope")
        or proposed["page_scope"]
    )
    proposed["retrieval_purpose"] = (
        contract.get("retrieval_purpose")
        or proposed["retrieval_purpose"]
    )
    proposed["what_belongs_here"] = (
        "Atomic active facts routed to this page by entity and section. "
        "Review current fact evidence before narrowing this contract."
    )
    proposed["what_does_not_belong_here"] = (
        contract.get("what_does_not_belong_here")
        or proposed["what_does_not_belong_here"]
    )
    return proposed


def gardener_action_payload(candidate: dict[str, Any]) -> dict[str, Any]:
    action_type = str(candidate.get("action_type") or "")
    if action_type == "rehome_fact":
        return {
            "fact_id": candidate.get("fact_id"),
            "page_hint": candidate.get("destination_page_hint"),
        }
    if action_type == "edit_contract":
        return {"contract": candidate.get("contract")}
    return {"candidate": candidate}


def append_candidate(
    candidates: list[dict[str, Any]],
    candidate: dict[str, Any] | None,
    suppressed_candidate_keys: set[str],
) -> None:
    if candidate is None:
        return
    if str(candidate.get("candidate_key") or "") in suppressed_candidate_keys:
        return
    candidates.append(candidate)


def candidate_sort_key(candidate: dict[str, Any]) -> tuple[float, int, str]:
    action_priority = {
        "page_merge": 0,
        "rehome_fact": 1,
        "page_split": 2,
        "edit_contract": 3,
    }
    return (
        -float(candidate.get("score") or candidate.get("similarity") or 0.0),
        action_priority.get(str(candidate.get("action_type") or ""), 99),
        ",".join(str(path) for path in candidate.get("page_hints") or []),
    )


def candidate_key(
    action_type: str, page_hints: list[str], fact_ids: list[str] | None = None
) -> str:
    pages = ",".join(sorted(str(path) for path in page_hints if path))
    facts = ",".join(sorted(str(fact_id) for fact_id in fact_ids or [] if fact_id))
    return f"{action_type}:{pages}:{facts}"


def first_page_fact(page: dict[str, Any]) -> dict[str, Any] | None:
    facts = [fact for fact in page.get("facts") or [] if isinstance(fact, dict)]
    return facts[0] if facts else None


def validate_fact_for_page(
    fact: dict[str, Any], page_hint: str, contract: dict[str, Any]
) -> dict[str, Any]:
    return validate_fact_against_contract({**fact, "page_hint": page_hint}, contract)


def merge_contract_check(
    left: dict[str, Any],
    right: dict[str, Any],
    contracts: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    left_hint = str(left["relative_path"])
    right_hint = str(right["relative_path"])
    left_contract = contracts.get(left_hint)
    right_contract = contracts.get(right_hint)
    if left_contract is None and right_contract is None:
        return {"compatible": True, "reason": "no contracts to compare"}
    invalid: list[dict[str, Any]] = []
    if right_contract is not None:
        invalid.extend(
            {
                "direction": f"{left_hint} -> {right_hint}",
                **validation,
            }
            for validation in validate_page_facts_for_contract(left, right_hint, right_contract)
            if not validation.get("valid")
        )
    if left_contract is not None:
        invalid.extend(
            {
                "direction": f"{right_hint} -> {left_hint}",
                **validation,
            }
            for validation in validate_page_facts_for_contract(right, left_hint, left_contract)
            if not validation.get("valid")
        )
    hard_invalid = [
        validation
        for validation in invalid
        if validation.get("reason") == "fact content overlaps contract exclusions"
    ]
    if hard_invalid:
        return {
            "compatible": False,
            "reason": "one page contract excludes the other page's facts",
            "invalid": hard_invalid[:5],
        }
    return {
        "compatible": True,
        "reason": "contracts do not explicitly exclude merge evidence",
        "invalid": invalid[:5],
    }


def validate_page_facts_for_contract(
    page: dict[str, Any], page_hint: str, contract: dict[str, Any]
) -> list[dict[str, Any]]:
    return [
        validate_fact_for_page(fact, page_hint, contract)
        for fact in page.get("facts") or []
        if isinstance(fact, dict)
    ]


def recent_suppressed_candidate_keys(paths: BrainPaths) -> set[str]:
    placeholders = ",".join("?" for _ in SUPPRESSED_GARDENER_STATUSES)
    keys: set[str] = set()
    with connection(paths.sqlite_path) as conn:
        for row in conn.execute(
            f"""
            SELECT action_features, evidence_json
            FROM cos_actions
            WHERE proposed_by = 'gardener'
              AND status IN ({placeholders})
            ORDER BY created_at DESC
            LIMIT 200
            """,
            SUPPRESSED_GARDENER_STATUSES,
        ):
            features = loads(row["action_features"], {})
            if isinstance(features, dict) and features.get("candidate_key"):
                keys.add(str(features["candidate_key"]))
                continue
            evidence = loads(row["evidence_json"], {})
            payload = evidence.get("payload") if isinstance(evidence, dict) else {}
            candidate = payload.get("candidate") if isinstance(payload, dict) else {}
            if isinstance(candidate, dict) and candidate.get("candidate_key"):
                keys.add(str(candidate["candidate_key"]))
    return keys


def tokenize_signal(*values: Any) -> set[str]:
    tokens: set[str] = set()
    for value in values:
        if value is None:
            continue
        if isinstance(value, (list, tuple, set)):
            text = " ".join(str(item) for item in value)
        else:
            text = str(value)
        normalized = text.lower().replace("_", " ").replace("/", " ").replace("-", " ")
        for token in TOKEN_RE.findall(normalized):
            if token in STOP_TOKENS:
                continue
            tokens.add(token)
    return tokens


def token_containment(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / min(len(left), len(right))


def stable_page_slug(page_hint: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", Path(page_hint).with_suffix("").as_posix().lower())
    return slug.strip("-")[:80] or "page"


def human_title_for_candidate(page_hint: str) -> str:
    stem = Path(page_hint).with_suffix("").name
    return stem.replace("-", " ").replace("_", " ").title()
