from __future__ import annotations

import os
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from .contracts import active_page_contracts, validate_fact_against_contract
from .cos_actions import propose_action
from .cos_policy import NEAR_DUPLICATE_PAGE_MERGE_SIGNAL, classify_action_risk
from .curation_settings import load_curation_settings, normalize_topology_review_threshold
from .db import connection, loads
from .entities import normalize_entity_name, normalize_entity_type
from .llm import (
    LLMConfigurationError,
    LLMProvider,
    complete_json,
    cos_role_provider_configured,
    get_cos_role_provider,
)
from .llm_usage import configure_provider_usage
from .paths import BrainPaths
from .wiki_facts import managed_fact_page_summaries


MERGE_PATH_SIMILARITY_FLOOR = 0.86
MERGE_EVIDENCE_OVERLAP_FLOOR = 0.25
MERGE_EVIDENCE_ONLY_PATH_FLOOR = 0.60
MERGE_STRONG_EVIDENCE_OVERLAP_FLOOR = 0.45
ENTITY_NAME_SIMILARITY_FLOOR = 0.88
HIGH_CERTAINTY_ENTITY_MERGE_SIGNALS = {
    "same_normalized_name_or_alias",
    "same_compact_name_or_alias",
}
PAGE_SPLIT_FACT_FLOOR = 5
PAGE_SPLIT_SECTION_FLOOR = 3
REHOME_DESTINATION_SCORE_FLOOR = 0.35
SUPPRESSED_GARDENER_STATUSES = ("failed", "reverted", "rejected", "dismissed")
OPEN_GARDENER_STATUSES = ("proposed", "needs_human")
DEFAULT_GARDENER_JUDGMENT_CANDIDATE_LIMIT = 100
DEFAULT_GARDENER_JUDGMENT_WORKERS = 4
DEFAULT_GARDENER_JUDGMENT_TIMEOUT_SECONDS = 120
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


def normalize_aggressiveness(value: float) -> float:
    parsed = float(value)
    if not 0.0 <= parsed <= 1.0:
        raise ValueError("topology aggressiveness must be between 0 and 1")
    return round(parsed, 2)


def merge_admission_thresholds(aggressiveness: float) -> dict[str, float]:
    normalized = normalize_aggressiveness(aggressiveness)
    offset = normalized - 0.5
    conservatism = max(0.0, (0.5 - normalized) * 2)
    return {
        "path_similarity_floor": round(
            min(0.96, max(0.76, MERGE_PATH_SIMILARITY_FLOOR - (0.2 * offset))),
            4,
        ),
        "evidence_overlap_floor": round(
            min(0.35, max(0.15, MERGE_EVIDENCE_OVERLAP_FLOOR - (0.2 * offset))),
            4,
        ),
        "evidence_only_path_floor": round(
            min(
                0.70,
                max(0.50, MERGE_EVIDENCE_ONLY_PATH_FLOOR - (0.2 * offset)),
            ),
            4,
        ),
        "strong_evidence_overlap_floor": round(
            min(
                0.55,
                max(
                    0.35,
                    MERGE_STRONG_EVIDENCE_OVERLAP_FLOOR - (0.2 * offset),
                ),
            ),
            4,
        ),
        "entity_name_similarity_floor": round(
            min(
                0.96,
                max(0.80, ENTITY_NAME_SIMILARITY_FLOOR - (0.16 * offset)),
            ),
            4,
        ),
        "entity_evidence_overlap_floor": round(
            min(0.25, max(0.15, 0.2 - (0.1 * offset))), 4
        ),
        "entity_containment_similarity_floor": round(
            0.72 + (0.24 * conservatism), 4
        ),
        "entity_containment_evidence_floor": round(0.3 * conservatism, 4),
    }


def split_admission_thresholds(aggressiveness: float) -> dict[str, int]:
    normalized = normalize_aggressiveness(aggressiveness)
    if normalized < 0.25:
        minimum_facts_per_section = 3
    elif normalized < 0.5:
        minimum_facts_per_section = 2
    else:
        minimum_facts_per_section = 1
    return {
        "fact_floor": max(3, min(12, round(12 - (14 * normalized)))),
        "section_floor": max(2, min(5, round(5 - (4 * normalized)))),
        "minimum_facts_per_section": minimum_facts_per_section,
    }


def generate_gardener_candidates(
    paths: BrainPaths,
    *,
    shadow: bool = True,
    max_candidates: int = 25,
    llm_provider: LLMProvider | None = None,
    provider: str | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    curation = load_curation_settings(paths)
    merge_aggressiveness = float(curation["merge_aggressiveness"])
    split_aggressiveness = float(curation["split_aggressiveness"])
    topology_review_threshold = int(curation["topology_review_threshold"])
    pages = enrich_gardener_pages(paths, managed_fact_page_summaries(paths))
    contracts = {contract["page_hint"]: contract for contract in active_page_contracts(paths)}
    suppressed_keys = recent_suppressed_candidate_keys(paths)
    candidates = deterministic_topology_candidates(
        pages,
        contracts,
        suppressed_candidate_keys=suppressed_keys,
        merge_aggressiveness=merge_aggressiveness,
        split_aggressiveness=split_aggressiveness,
        topology_review_threshold=topology_review_threshold,
    )
    candidates.extend(
        deterministic_entity_candidates(
            paths,
            suppressed_candidate_keys=suppressed_keys,
            merge_aggressiveness=merge_aggressiveness,
            topology_review_threshold=topology_review_threshold,
        )
    )
    candidates.sort(key=candidate_sort_key)
    deterministic_candidate_count = len(candidates)
    judgment_limit = max(max_candidates, gardener_judgment_candidate_limit())
    judgment_candidates = candidates[:judgment_limit]
    llm_result = apply_gardener_judgment(
        judgment_candidates,
        pages,
        contracts,
        paths=paths,
        llm_provider=llm_provider,
        provider=provider,
        usage_cycle_id=run_id,
    )
    judged_candidates = llm_result["candidates"]
    prioritized_candidates, arbitration = prioritize_topology_candidates(
        judged_candidates
    )
    candidates = prioritized_candidates[:max_candidates]
    truncated_kept_candidates = prioritized_candidates[max_candidates:]
    llm_summary = dict(llm_result["summary"])
    llm_summary["topology_arbitration"] = arbitration
    llm_summary["truncated_kept_candidate_count"] = len(truncated_kept_candidates)
    llm_summary["truncated_kept_candidates"] = [
        gardener_candidate_audit_card(
            candidate,
            judgment=candidate.get("llm_judgment") if isinstance(candidate.get("llm_judgment"), dict) else None,
        )
        for candidate in truncated_kept_candidates
    ]
    actions: list[dict[str, Any]] = []
    if not shadow:
        for candidate in candidates:
            actions.append(
                propose_gardener_action(
                    paths, candidate, decide=True, run_id=run_id
                )
            )
    return {
        "status": "ok",
        "shadow": shadow,
        "candidate_count": len(candidates),
        "deterministic_candidate_count": deterministic_candidate_count,
        "judgment_candidate_count": len(judgment_candidates),
        "suppressed_candidate_count": len(suppressed_keys),
        "topology_settings": {
            "merge_aggressiveness": merge_aggressiveness,
            "split_aggressiveness": split_aggressiveness,
            "topology_review_threshold": topology_review_threshold,
        },
        "llm_judgment": llm_summary,
        "candidates": candidates,
        "actions": actions,
    }


def deterministic_topology_candidates(
    pages: list[dict[str, Any]],
    contracts: dict[str, dict[str, Any]],
    *,
    suppressed_candidate_keys: set[str] | None = None,
    merge_aggressiveness: float = 0.5,
    split_aggressiveness: float = 0.5,
    topology_review_threshold: int = 8,
) -> list[dict[str, Any]]:
    suppressed_candidate_keys = suppressed_candidate_keys or set()
    candidates: list[dict[str, Any]] = []
    for index, left in enumerate(pages):
        for right in pages[index + 1 :]:
            append_candidate(
                candidates,
                merge_candidate(
                    left,
                    right,
                    contracts,
                    merge_aggressiveness=merge_aggressiveness,
                    topology_review_threshold=topology_review_threshold,
                ),
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
        append_candidate(
            candidates,
            page_split_candidate(
                page,
                contracts,
                split_aggressiveness=split_aggressiveness,
                topology_review_threshold=topology_review_threshold,
            ),
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


def deterministic_entity_candidates(
    paths: BrainPaths,
    *,
    suppressed_candidate_keys: set[str] | None = None,
    merge_aggressiveness: float = 0.5,
    topology_review_threshold: int = 8,
) -> list[dict[str, Any]]:
    suppressed_candidate_keys = suppressed_candidate_keys or set()
    entities = load_entity_gardener_evidence(paths)
    candidates: list[dict[str, Any]] = []
    for index, left in enumerate(entities):
        for right in entities[index + 1 :]:
            append_candidate(
                candidates,
                entity_merge_candidate(
                    left,
                    right,
                    merge_aggressiveness=merge_aggressiveness,
                    topology_review_threshold=topology_review_threshold,
                ),
                suppressed_candidate_keys,
            )
    candidates.sort(key=candidate_sort_key)
    return candidates


def load_entity_gardener_evidence(paths: BrainPaths) -> list[dict[str, Any]]:
    with connection(paths.sqlite_path) as conn:
        entity_rows = [
            dict(row)
            for row in conn.execute(
                """
                SELECT *
                FROM entities
                WHERE COALESCE(status, 'active') = 'active'
                ORDER BY created_at, id
                """
            )
        ]
        if not entity_rows:
            return []
        entity_ids = [str(row["id"]) for row in entity_rows]
        placeholders = ",".join("?" for _ in entity_ids)
        links = list(
            conn.execute(
                f"""
                SELECT fe.entity_id, fe.fact_id, fe.is_primary, fe.mention_text,
                       f.statement, f.page_hint, f.source_ids, f.status
                FROM fact_entities fe
                LEFT JOIN facts f ON f.id = fe.fact_id
                WHERE fe.entity_id IN ({placeholders})
                ORDER BY fe.entity_id, fe.is_primary DESC, fe.fact_id
                """,
                entity_ids,
            )
        )
    evidence: dict[str, dict[str, Any]] = {
        str(row["id"]): {
            **row,
            "aliases_list": [
                str(alias)
                for alias in loads(row.get("aliases"), [])
                if str(alias or "").strip()
            ],
            "source_ids_list": [
                str(source_id)
                for source_id in loads(row.get("source_ids"), [])
                if str(source_id or "").strip()
            ],
            "fact_ids": set(),
            "active_fact_ids": set(),
            "primary_fact_ids": set(),
            "page_hints": set(),
            "mention_texts": set(),
            "fact_tokens": set(),
            "source_ids": set(),
        }
        for row in entity_rows
    }
    for link in links:
        entity_id = str(link["entity_id"])
        bucket = evidence.get(entity_id)
        if bucket is None:
            continue
        fact_id = str(link["fact_id"] or "")
        if fact_id:
            bucket["fact_ids"].add(fact_id)
        if link["is_primary"] and fact_id:
            bucket["primary_fact_ids"].add(fact_id)
        mention_text = str(link["mention_text"] or "").strip()
        if mention_text:
            bucket["mention_texts"].add(mention_text)
        source_ids = loads(link["source_ids"], []) if link["source_ids"] else []
        if not isinstance(source_ids, list):
            source_ids = []
        bucket["source_ids"].update(str(source_id) for source_id in source_ids if source_id)
        if link["status"] == "active":
            if fact_id:
                bucket["active_fact_ids"].add(fact_id)
            if link["page_hint"]:
                bucket["page_hints"].add(str(link["page_hint"]))
            bucket["fact_tokens"].update(
                tokenize_signal(
                    link["statement"],
                    link["page_hint"],
                    source_ids,
                    mention_text,
                )
            )
    for item in evidence.values():
        item["source_ids"].update(item["source_ids_list"])
    output: list[dict[str, Any]] = []
    for item in evidence.values():
        names = [str(item["name"]), *item["aliases_list"], *sorted(item["mention_texts"])]
        item["name_keys"] = sorted({normalize_entity_name(name) for name in names if name})
        item["compact_keys"] = sorted({compact_entity_key(name) for name in names if name})
        item["fact_ids"] = sorted(item["fact_ids"])
        item["active_fact_ids"] = sorted(item["active_fact_ids"])
        item["primary_fact_ids"] = sorted(item["primary_fact_ids"])
        item["page_hints"] = sorted(item["page_hints"])
        item["source_ids"] = sorted(item["source_ids"])
        item["mention_texts"] = sorted(item["mention_texts"])
        item["fact_tokens"] = sorted(item["fact_tokens"])
        item["entity_type"] = normalize_entity_type(item.get("entity_type"))
        output.append(item)
    return output


def entity_merge_candidate(
    left: dict[str, Any],
    right: dict[str, Any],
    *,
    merge_aggressiveness: float = 0.5,
    topology_review_threshold: int = 8,
) -> dict[str, Any] | None:
    left_type = normalize_entity_type(left.get("entity_type"))
    right_type = normalize_entity_type(right.get("entity_type"))
    if left_type and right_type and left_type != right_type:
        return None
    if not (left.get("fact_ids") or right.get("fact_ids")):
        return None

    signal = entity_merge_signal(
        left, right, merge_aggressiveness=merge_aggressiveness
    )
    if signal is None:
        return None
    canonical, merged = choose_canonical_entity(left, right)
    merged_fact_ids = [str(fact_id) for fact_id in merged.get("fact_ids") or [] if fact_id]
    all_fact_ids = stable_unique(
        [
            *[str(fact_id) for fact_id in canonical.get("fact_ids") or [] if fact_id],
            *merged_fact_ids,
        ]
    )
    page_hints = stable_unique(
        [
            *[str(path) for path in canonical.get("page_hints") or [] if path],
            *[str(path) for path in merged.get("page_hints") or [] if path],
        ]
    )
    affected_fact_count = len(all_fact_ids)
    topology_review_threshold = normalize_topology_review_threshold(
        topology_review_threshold
    )
    large_topology = affected_fact_count >= topology_review_threshold
    risk_tier = (
        signal["risk_tier"]
        if signal["merge_signal"] in HIGH_CERTAINTY_ENTITY_MERGE_SIGNALS
        else "high" if large_topology else signal["risk_tier"]
    )
    entity_ids = [str(canonical["id"]), str(merged["id"])]
    return {
        "action_type": "entity_merge",
        "entity_ids": entity_ids,
        "canonical_entity_id": str(canonical["id"]),
        "merged_entity_ids": [str(merged["id"])],
        "entity_names": {
            str(canonical["id"]): str(canonical["name"]),
            str(merged["id"]): str(merged["name"]),
        },
        "entity_types": {
            str(canonical["id"]): normalize_entity_type(canonical.get("entity_type")),
            str(merged["id"]): normalize_entity_type(merged.get("entity_type")),
        },
        "fact_ids": all_fact_ids,
        "page_hints": page_hints,
        "candidate_key": entity_candidate_key("entity_merge", entity_ids),
        "score": signal["score"],
        "similarity": signal["similarity"],
        "reason": signal["reason"],
        "merge_signal": signal["merge_signal"],
        "affected_fact_count": affected_fact_count,
        "merged_entity_count": 2,
        "large_topology": large_topology,
        "topology_review_threshold": topology_review_threshold,
        "cross_entity_merge": False,
        "cross_type_merge": False,
        "type_mismatch": False,
        "risk_tier": risk_tier,
        "merge_aggressiveness": normalize_aggressiveness(merge_aggressiveness),
    }


def entity_merge_signal(
    left: dict[str, Any],
    right: dict[str, Any],
    *,
    merge_aggressiveness: float = 0.5,
) -> dict[str, Any] | None:
    thresholds = merge_admission_thresholds(merge_aggressiveness)
    left_name_keys = set(left.get("name_keys") or [])
    right_name_keys = set(right.get("name_keys") or [])
    left_compact = set(left.get("compact_keys") or [])
    right_compact = set(right.get("compact_keys") or [])
    similarity = max_entity_name_similarity(left_name_keys, right_name_keys)
    shared_sources = set(left.get("source_ids") or []) & set(right.get("source_ids") or [])
    fact_overlap = token_containment(
        set(left.get("fact_tokens") or []),
        set(right.get("fact_tokens") or []),
    )
    if left_name_keys & right_name_keys:
        return {
            "score": 0.98,
            "similarity": round(max(similarity, 0.98), 4),
            "merge_signal": "same_normalized_name_or_alias",
            "reason": "entities share a normalized name or alias",
            "risk_tier": "low",
        }
    compact_overlap = {key for key in left_compact & right_compact if key}
    if compact_overlap:
        return {
            "score": 0.94,
            "similarity": round(max(similarity, 0.94), 4),
            "merge_signal": "same_compact_name_or_alias",
            "reason": "entity names differ only by spacing or punctuation",
            "risk_tier": "low",
        }
    if entity_name_containment(left_name_keys, right_name_keys):
        normalized = normalize_aggressiveness(merge_aggressiveness)
        containment_evidence_floor = thresholds[
            "entity_containment_evidence_floor"
        ]
        if normalized < 0.5 and (
            similarity < thresholds["entity_containment_similarity_floor"]
            or not (
                shared_sources or fact_overlap >= containment_evidence_floor
            )
        ):
            return None
        score = round(min(0.9, max(0.72, similarity, 0.62 + (0.2 * fact_overlap))), 4)
        return {
            "score": score,
            "similarity": round(similarity, 4),
            "merge_signal": "name_containment",
            "reason": "one entity name contains the other",
            "risk_tier": "medium",
        }
    if similarity >= thresholds["entity_name_similarity_floor"] and (
        shared_sources or fact_overlap >= thresholds["entity_evidence_overlap_floor"]
    ):
        score = round(min(0.9, max(similarity, 0.65 + (0.25 * fact_overlap))), 4)
        return {
            "score": score,
            "similarity": round(similarity, 4),
            "merge_signal": "near_name_with_evidence_overlap",
            "reason": "similar entity names share source or fact-token evidence",
            "risk_tier": "medium",
        }
    return None


def choose_canonical_entity(
    left: dict[str, Any],
    right: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    ordered = sorted(
        [left, right],
        key=lambda row: (
            -len(row.get("primary_fact_ids") or []),
            -len(row.get("fact_ids") or []),
            len(str(row.get("name") or "")),
            str(row.get("name") or "").casefold(),
            str(row.get("id") or ""),
        ),
    )
    return ordered[0], ordered[1]


def max_entity_name_similarity(left_keys: set[str], right_keys: set[str]) -> float:
    best = 0.0
    for left in left_keys:
        for right in right_keys:
            if not left or not right:
                continue
            best = max(best, SequenceMatcher(None, left, right).ratio())
    return best


def entity_name_containment(left_keys: set[str], right_keys: set[str]) -> bool:
    for left in left_keys:
        left_tokens = set(left.split())
        if not left_tokens:
            continue
        for right in right_keys:
            right_tokens = set(right.split())
            if not right_tokens or left_tokens == right_tokens:
                continue
            if left_tokens < right_tokens or right_tokens < left_tokens:
                return True
    return False


def compact_entity_key(value: Any) -> str:
    return re.sub(r"[^0-9a-z]+", "", normalize_entity_name(value))


def propose_gardener_action(
    paths: BrainPaths,
    candidate: dict[str, Any],
    *,
    decide: bool = False,
    run_id: str | None = None,
) -> dict[str, Any]:
    spec = gardener_action_spec(candidate)
    return propose_action(
        paths,
        spec["action_type"],
        action_payload=spec["action_payload"],
        evidence=spec["evidence"],
        action_features=spec["action_features"],
        run_id=run_id,
        target_fact_ids=spec["target_fact_ids"],
        target_page_paths=spec["target_page_paths"],
        target_contract_ids=spec["target_contract_ids"],
        proposed_by=spec["proposed_by"],
        confidence=spec["confidence"],
        risk_tier=spec["risk_tier"],
        decide=decide,
    )


def gardener_action_spec(candidate: dict[str, Any]) -> dict[str, Any]:
    action_type = str(candidate["action_type"])
    contract = candidate.get("contract") if isinstance(candidate.get("contract"), dict) else None
    contract_check = (
        candidate.get("contract_check")
        if isinstance(candidate.get("contract_check"), dict)
        else {}
    )
    judgment = (
        candidate.get("llm_judgment")
        if isinstance(candidate.get("llm_judgment"), dict)
        else {}
    )
    target_fact_ids = (
        [str(fact_id) for fact_id in candidate.get("fact_ids") or [] if fact_id]
        or ([str(candidate["fact_id"])] if candidate.get("fact_id") else [])
    )
    target_contract_ids = [str(contract["id"])] if contract and contract.get("id") else []
    action_features = {
        "candidate_key": candidate.get("candidate_key"),
        "candidate_signal": candidate.get("reason"),
        "score": candidate.get("score"),
        "similarity": candidate.get("similarity"),
        "evidence_overlap": candidate.get("evidence_overlap"),
        "fact_token_overlap": candidate.get("fact_token_overlap"),
        "shared_source_count": int(candidate.get("shared_source_count") or 0),
        "shared_entity_count": len(candidate.get("shared_entity_keys") or []),
        "affected_fact_count": int(candidate.get("affected_fact_count") or len(target_fact_ids)),
        "affected_page_count": len(candidate.get("page_hints") or []),
        "merged_entity_count": candidate.get("merged_entity_count"),
        "large_topology": candidate.get("large_topology"),
        "cross_entity_merge": bool(candidate.get("cross_entity_merge")),
        "cross_type_merge": bool(candidate.get("cross_type_merge")),
        "type_mismatch": bool(candidate.get("type_mismatch")),
        "contract_compatible": bool(contract_check.get("compatible")),
        "duplicate_page_merge_signal": (
            action_type == "page_merge"
            and candidate.get("reason") == NEAR_DUPLICATE_PAGE_MERGE_SIGNAL
        ),
        "gardener_confirmed": (
            judgment.get("decision") == "keep"
            and str(candidate.get("risk_tier") or "").lower() in {"low", "medium"}
            and not bool(candidate.get("needs_review"))
            and candidate.get("gardener_review_status") != "llm_judgment_failed"
            and not judgment.get("error_type")
        ),
        "merge_signal": candidate.get("merge_signal"),
        "merge_aggressiveness": candidate.get("merge_aggressiveness"),
        "split_aggressiveness": candidate.get("split_aggressiveness"),
        "topology_review_threshold": candidate.get("topology_review_threshold"),
        "admission_thresholds": candidate.get("admission_thresholds"),
        "reversible": True,
        "truth_mutation": False,
    }
    if action_type != "entity_merge":
        action_features["eval_gate"] = {"suite": "topology"}
    confidence = float(candidate.get("score") or candidate.get("similarity") or 0.0)
    action_features.update(
        {
            "confidence": confidence,
            "target_fact_ids": target_fact_ids,
            "target_page_paths": [
                str(path) for path in candidate.get("page_hints") or []
            ],
            "target_contract_ids": target_contract_ids,
        }
    )
    risk_tier = classify_action_risk(
        action_type,
        action_features,
        explicit_risk_tier=str(candidate.get("risk_tier") or "medium"),
    )
    action_features["risk_tier"] = risk_tier
    return {
        "action_type": action_type,
        "action_payload": gardener_action_payload(candidate),
        "evidence": {
            "gardener_judgment": candidate.get("llm_judgment"),
        }
        if candidate.get("llm_judgment")
        else None,
        "action_features": action_features,
        "target_fact_ids": target_fact_ids,
        "target_page_paths": action_features["target_page_paths"],
        "target_contract_ids": target_contract_ids,
        "proposed_by": (
            "gardener_llm" if candidate.get("llm_judgment") else "gardener"
        ),
        "confidence": confidence,
        "risk_tier": risk_tier,
    }


def prioritize_topology_candidates(
    candidates: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    ordered = sorted(candidates, key=candidate_sort_key)
    selected: list[dict[str, Any]] = []
    reserved_pages: set[str] = set()
    reserved_entities: set[str] = set()
    suppressed: Counter[str] = Counter()

    for candidate in ordered:
        action_type = str(candidate.get("action_type") or "")
        if action_type == "page_merge":
            pages = {str(item) for item in candidate.get("page_hints") or [] if item}
            if pages & reserved_pages:
                suppressed["overlapping_page_merge"] += 1
                continue
            reserved_pages.update(pages)
            selected.append(candidate)
            continue
        if action_type == "entity_merge":
            entity_ids = {
                str(item) for item in candidate.get("entity_ids") or [] if item
            }
            if entity_ids & reserved_entities:
                suppressed["overlapping_entity_merge"] += 1
                continue
            reserved_entities.update(entity_ids)
            selected.append(candidate)
            continue
        selected.append(candidate)

    output: list[dict[str, Any]] = []
    for candidate in selected:
        if str(candidate.get("action_type") or "") == "page_split":
            pages = {str(item) for item in candidate.get("page_hints") or [] if item}
            if pages & reserved_pages:
                suppressed["split_conflicts_with_preferred_merge"] += 1
                continue
        output.append(candidate)
    output.sort(key=candidate_sort_key)
    return output, {
        "input_count": len(candidates),
        "selected_count": len(output),
        "suppressed_count": len(candidates) - len(output),
        "suppressed_by_reason": dict(sorted(suppressed.items())),
    }


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
    usage_cycle_id: str | None = None,
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

    timeout_seconds = gardener_judgment_timeout_seconds()
    worker_count = gardener_judgment_worker_count(len(candidates), shared_provider=llm_provider is not None)
    prepared_jobs = prepare_gardener_judgment_jobs(
        candidates,
        paths=paths,
        llm_provider=llm_provider,
        provider=provider,
        timeout_seconds=timeout_seconds,
        worker_count=worker_count,
        usage_cycle_id=usage_cycle_id,
    )
    results = run_gardener_judgment_jobs(
        prepared_jobs,
        pages,
        contracts,
        worker_count=worker_count,
    )
    kept: list[dict[str, Any]] = []
    dropped = 0
    dropped_candidates: list[dict[str, Any]] = []
    judgment_count = 0
    needs_review_count = 0
    error_count = 0
    timeout_count = 0
    effort_counts: Counter[str] = Counter()
    for result in results:
        candidate = result["candidate"]
        effort_counts[str(result.get("reasoning_effort") or "unknown")] += 1
        if result.get("error") is not None:
            error = result["error"]
            error_count += 1
            if "timed out" in str(error).lower() or "timeout" in type(error).__name__.lower():
                timeout_count += 1
            kept.append(gardener_candidate_needs_review(candidate, error))
            needs_review_count += 1
            continue
        judgment = normalize_gardener_judgment(result.get("judgment"))
        if judgment is None:
            kept.append(candidate)
            continue
        judgment_count += 1
        if judgment["decision"] == "drop":
            dropped += 1
            dropped_candidates.append(gardener_candidate_audit_card(candidate, judgment=judgment))
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
            "mode": "per_candidate",
            "candidate_input_count": len(candidates),
            "judgment_count": judgment_count,
            "dropped_candidate_count": dropped,
            "dropped": dropped_candidates,
            "needs_review_count": needs_review_count,
            "error_count": error_count,
            "timeout_count": timeout_count,
            "worker_count": worker_count,
            "timeout_seconds": timeout_seconds,
            "effort_counts": dict(sorted(effort_counts.items())),
        },
    }


def prepare_gardener_judgment_jobs(
    candidates: list[dict[str, Any]],
    *,
    paths: BrainPaths | None,
    llm_provider: LLMProvider | None,
    provider: str | None,
    timeout_seconds: int,
    worker_count: int,
    usage_cycle_id: str | None,
) -> list[dict[str, Any]]:
    grouped: dict[str, list[tuple[int, dict[str, Any]]]] = {}
    for index, candidate in enumerate(candidates):
        effort = gardener_candidate_reasoning_effort(candidate)
        grouped.setdefault(effort, []).append((index, candidate))
    jobs: list[dict[str, Any]] = []
    for effort in sorted(grouped, key=gardener_reasoning_effort_sort_key):
        items = grouped[effort]
        buckets: list[list[tuple[int, dict[str, Any]]]] = [
            [] for _ in range(max(1, min(worker_count, len(items))))
        ]
        for offset, item in enumerate(items):
            buckets[offset % len(buckets)].append(item)
        for bucket in buckets:
            try:
                active_provider = gardener_candidate_provider(
                    paths=paths,
                    llm_provider=llm_provider,
                    provider=provider,
                    timeout_seconds=timeout_seconds,
                    reasoning_effort=effort,
                    usage_cycle_id=usage_cycle_id,
                )
                jobs.append(
                    {
                        "items": bucket,
                        "provider": active_provider,
                        "error": None,
                        "reasoning_effort": effort,
                    }
                )
            except Exception as exc:  # noqa: BLE001 - per-candidate failure must not abort the run.
                jobs.append(
                    {
                        "items": bucket,
                        "provider": None,
                        "error": exc,
                        "reasoning_effort": effort,
                    }
                )
    return jobs


def gardener_candidate_provider(
    *,
    paths: BrainPaths | None,
    llm_provider: LLMProvider | None,
    provider: str | None,
    timeout_seconds: int,
    reasoning_effort: str,
    usage_cycle_id: str | None,
) -> LLMProvider:
    active_paths = paths or BrainPaths.from_value(None)
    active_provider = get_cos_role_provider(
        active_paths,
        "gardener",
        provider=provider,
        llm_provider=llm_provider,
    )
    if active_provider is None:
        raise LLMConfigurationError("No CoS LLM provider configured for role: gardener")
    configure_provider_usage(
        active_provider,
        active_paths,
        "gardener",
        cycle_id=usage_cycle_id,
        run_id=usage_cycle_id,
        stage="cos_gardener",
    )
    if hasattr(active_provider, "timeout"):
        setattr(active_provider, "timeout", timeout_seconds)
    if hasattr(active_provider, "reasoning_effort"):
        setattr(active_provider, "reasoning_effort", reasoning_effort)
    return active_provider


def run_gardener_judgment_jobs(
    jobs: list[dict[str, Any]],
    pages: list[dict[str, Any]],
    contracts: dict[str, dict[str, Any]],
    *,
    worker_count: int,
) -> list[dict[str, Any]]:
    indexed_results: list[tuple[int, dict[str, Any]]] = []
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = {
            executor.submit(
                run_gardener_judgment_job,
                job,
                pages,
                contracts,
            ): index
            for index, job in enumerate(jobs)
        }
        for future in as_completed(futures):
            job = jobs[futures[future]]
            try:
                indexed_results.extend(future.result())
            except Exception as exc:  # noqa: BLE001 - one worker failure should become review residue.
                for index, candidate in job["items"]:
                    indexed_results.append(
                        (
                            index,
                            {
                                "candidate": candidate,
                                "judgment": None,
                                "error": exc,
                                "reasoning_effort": job.get("reasoning_effort"),
                            },
                        )
                    )
    return [result for _, result in sorted(indexed_results, key=lambda item: item[0])]


def run_gardener_judgment_job(
    job: dict[str, Any],
    pages: list[dict[str, Any]],
    contracts: dict[str, dict[str, Any]],
) -> list[tuple[int, dict[str, Any]]]:
    return [
        (
            index,
            judge_gardener_candidate(
                candidate,
                pages,
                contracts,
                job.get("provider"),
                job.get("error"),
                str(job.get("reasoning_effort") or "unknown"),
            ),
        )
        for index, candidate in job["items"]
    ]


def judge_gardener_candidate(
    candidate: dict[str, Any],
    pages: list[dict[str, Any]],
    contracts: dict[str, dict[str, Any]],
    provider: LLMProvider | None,
    provider_error: Exception | None,
    reasoning_effort: str,
) -> dict[str, Any]:
    if provider_error is not None:
        return {
            "candidate": candidate,
            "judgment": None,
            "error": provider_error,
            "reasoning_effort": reasoning_effort,
        }
    try:
        parsed = complete_json(
            gardener_judgment_prompt([candidate], pages, contracts, fact_statement_limit=3),
            schema=GARDENER_JUDGMENT_SCHEMA,
            llm_provider=provider,
            max_attempts=2,
        )
    except Exception as exc:  # noqa: BLE001 - one bad candidate should become review residue.
        return {
            "candidate": candidate,
            "judgment": None,
            "error": exc,
            "reasoning_effort": reasoning_effort,
        }
    key = str(candidate.get("candidate_key") or "")
    judgments = {
        str(item.get("candidate_key") or ""): item
        for item in parsed.get("judgments") or []
        if isinstance(item, dict) and item.get("candidate_key")
    }
    return {
        "candidate": candidate,
        "judgment": judgments.get(key),
        "error": None,
        "reasoning_effort": reasoning_effort,
    }


def gardener_candidate_reasoning_effort(candidate: dict[str, Any]) -> str:
    merge_signal = str(candidate.get("merge_signal") or "")
    if merge_signal in HIGH_CERTAINTY_ENTITY_MERGE_SIGNALS and str(candidate.get("risk_tier") or "").lower() == "low":
        return "low"
    if (
        candidate.get("action_type") == "page_merge"
        and candidate.get("reason") == NEAR_DUPLICATE_PAGE_MERGE_SIGNAL
        and bool((candidate.get("contract_check") or {}).get("compatible"))
    ):
        return "medium"
    if bool(candidate.get("large_topology")) or bool(candidate.get("cross_entity_merge")):
        return "xhigh"
    if bool(candidate.get("cross_type_merge")) or bool(candidate.get("type_mismatch")):
        return "xhigh"
    if merge_signal in {"name_containment", "near_name_with_evidence_overlap"}:
        return "xhigh"
    if str(candidate.get("risk_tier") or "").lower() == "high":
        return "xhigh"
    if str(candidate.get("risk_tier") or "").lower() == "medium":
        return "medium"
    return "low"


def gardener_reasoning_effort_sort_key(effort: str) -> int:
    order = {"minimal": 0, "low": 1, "medium": 2, "high": 3, "xhigh": 4}
    return order.get(effort, 99)


def gardener_candidate_needs_review(candidate: dict[str, Any], error: Exception) -> dict[str, Any]:
    row = dict(candidate)
    row["risk_tier"] = "high"
    row["needs_review"] = True
    row["gardener_review_status"] = "llm_judgment_failed"
    row["llm_judgment"] = {
        "candidate_key": str(candidate.get("candidate_key") or ""),
        "decision": "keep",
        "rationale": (
            "Gardener LLM judgment failed for this candidate; preserve it for human review "
            f"instead of aborting the run. Error: {summarize_gardener_error(error)}"
        ),
        "risk_tier": "high",
        "needs_review": True,
        "error_type": type(error).__name__,
    }
    return row


def gardener_candidate_audit_card(
    candidate: dict[str, Any],
    *,
    judgment: dict[str, Any] | None = None,
) -> dict[str, Any]:
    card: dict[str, Any] = {
        "candidate_key": candidate.get("candidate_key"),
        "action_type": candidate.get("action_type"),
        "risk_tier": candidate.get("risk_tier"),
        "score": candidate.get("score"),
        "similarity": candidate.get("similarity"),
        "page_hints": candidate.get("page_hints"),
        "source_page_hint": candidate.get("source_page_hint"),
        "destination_page_hint": candidate.get("destination_page_hint"),
        "fact_id": candidate.get("fact_id"),
        "entity_ids": candidate.get("entity_ids"),
        "canonical_entity_id": candidate.get("canonical_entity_id"),
        "merged_entity_ids": candidate.get("merged_entity_ids"),
        "entity_names": candidate.get("entity_names"),
        "entity_types": candidate.get("entity_types"),
        "merge_signal": candidate.get("merge_signal"),
        "reason": candidate.get("reason"),
    }
    if judgment is not None:
        card["llm_judgment"] = judgment
    return {
        key: value
        for key, value in card.items()
        if value not in (None, "", [], {})
    }


def summarize_gardener_error(error: Exception) -> str:
    detail = str(error).strip() or type(error).__name__
    return detail[:300]


def gardener_judgment_candidate_limit() -> int:
    return configured_positive_int(
        "PKM_BRAIN_GARDENER_JUDGMENT_CANDIDATES",
        DEFAULT_GARDENER_JUDGMENT_CANDIDATE_LIMIT,
    )


def gardener_judgment_worker_count(candidate_count: int, *, shared_provider: bool) -> int:
    if candidate_count <= 1 or shared_provider:
        return 1
    configured = configured_positive_int(
        "PKM_BRAIN_GARDENER_JUDGMENT_WORKERS",
        DEFAULT_GARDENER_JUDGMENT_WORKERS,
    )
    return max(1, min(candidate_count, configured))


def gardener_judgment_timeout_seconds() -> int:
    return configured_positive_int(
        "PKM_BRAIN_GARDENER_JUDGMENT_TIMEOUT_SECONDS",
        DEFAULT_GARDENER_JUDGMENT_TIMEOUT_SECONDS,
    )


def configured_positive_int(name: str, default: int) -> int:
    raw_value = os.environ.get(name)
    if raw_value is None:
        return default
    try:
        parsed = int(raw_value)
    except ValueError:
        return default
    return parsed if parsed > 0 else default


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
    *,
    fact_statement_limit: int = 5,
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
            "entity_ids": candidate.get("entity_ids"),
            "canonical_entity_id": candidate.get("canonical_entity_id"),
            "merged_entity_ids": candidate.get("merged_entity_ids"),
            "entity_names": candidate.get("entity_names"),
            "entity_types": candidate.get("entity_types"),
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
            "fact_statements": list(page.get("fact_statements") or [])[:fact_statement_limit],
        }
        for page in pages
        if page.get("relative_path") in {
            page_hint
            for candidate in candidates
            for page_hint in candidate.get("page_hints") or []
        }
    ]
    entity_cards = [
        {
            "candidate_key": candidate.get("candidate_key"),
            "canonical_entity_id": candidate.get("canonical_entity_id"),
            "merged_entity_ids": candidate.get("merged_entity_ids"),
            "entity_names": candidate.get("entity_names"),
            "entity_types": candidate.get("entity_types"),
            "fact_ids": candidate.get("fact_ids"),
            "page_hints": candidate.get("page_hints"),
            "reason": candidate.get("reason"),
            "score": candidate.get("score"),
        }
        for candidate in candidates
        if candidate.get("action_type") == "entity_merge"
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
        "Candidates may be page topology changes or entity merges. "
        "You may only keep or drop existing candidate_key values; do not invent new candidates, page_hints, fact_ids, or entity_ids. "
        "Drop candidates that violate contracts, merge unrelated scopes, or lack enough evidence. "
        "Return exactly one JSON object shaped as {\"judgments\": [{\"candidate_key\": \"...\", \"decision\": \"keep\" or \"drop\", \"rationale\": \"...\"}]}. "
        "The top-level judgments array is required even when reviewing one candidate. "
        "Each judgment may also include score_adjustment and risk_tier.\n\n"
        f"Candidates:\n{candidate_cards}\n\nPages:\n{page_cards}\n\nEntities:\n{entity_cards}\n\nContracts:\n{contract_cards}"
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
    *,
    merge_aggressiveness: float = 0.5,
    topology_review_threshold: int = 8,
) -> dict[str, Any] | None:
    thresholds = merge_admission_thresholds(merge_aggressiveness)
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
        path_similarity >= thresholds["path_similarity_floor"]
        and evidence_overlap >= thresholds["evidence_overlap_floor"]
    )
    strong_evidence = (
        path_similarity >= thresholds["evidence_only_path_floor"]
        and evidence_overlap >= thresholds["strong_evidence_overlap_floor"]
    )
    if not (path_and_evidence or strong_evidence):
        return None
    page_hints = sorted([left_hint, right_hint])
    affected_fact_count = int(left.get("active_fact_count") or 0) + int(
        right.get("active_fact_count") or 0
    )
    topology_review_threshold = normalize_topology_review_threshold(
        topology_review_threshold
    )
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
        "reason": NEAR_DUPLICATE_PAGE_MERGE_SIGNAL,
        "contracts_present": [
            left_hint in contracts,
            right_hint in contracts,
        ],
        "affected_fact_count": affected_fact_count,
        "large_topology": affected_fact_count >= topology_review_threshold,
        "topology_review_threshold": topology_review_threshold,
        "risk_tier": "medium",
        "merge_aggressiveness": normalize_aggressiveness(merge_aggressiveness),
        "admission_thresholds": thresholds,
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
    page: dict[str, Any],
    contracts: dict[str, dict[str, Any]],
    *,
    split_aggressiveness: float = 0.5,
    topology_review_threshold: int = 8,
) -> dict[str, Any] | None:
    thresholds = split_admission_thresholds(split_aggressiveness)
    page_hint = str(page["relative_path"])
    section_counts = {
        str(section): int(count)
        for section, count in (page.get("section_counts") or {}).items()
        if section and str(section).lower() not in {"summary", "unsectioned"}
    }
    substantial_sections = {
        section: count
        for section, count in section_counts.items()
        if count >= thresholds["minimum_facts_per_section"]
    }
    active_count = int(page.get("active_fact_count") or 0)
    topology_review_threshold = normalize_topology_review_threshold(
        topology_review_threshold
    )
    if active_count < thresholds["fact_floor"] or len(substantial_sections) < thresholds[
        "section_floor"
    ]:
        return None
    score = round(min(0.95, 0.45 + (active_count / 25.0) + (len(section_counts) * 0.05)), 4)
    return {
        "action_type": "page_split",
        "page_hints": [page_hint],
        "candidate_key": candidate_key("page_split", [page_hint]),
        "score": score,
        "similarity": 0.0,
        "reason": "dense page has active facts across multiple sections",
        "section_counts": dict(sorted(section_counts.items())),
        "substantial_section_counts": dict(sorted(substantial_sections.items())),
        "contracts_present": [page_hint in contracts],
        "affected_fact_count": active_count,
        "large_topology": active_count >= topology_review_threshold,
        "topology_review_threshold": topology_review_threshold,
        "risk_tier": "medium",
        "split_aggressiveness": normalize_aggressiveness(split_aggressiveness),
        "admission_thresholds": thresholds,
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
    if action_type == "entity_merge":
        return {
            "canonical_entity_id": candidate.get("canonical_entity_id"),
            "merged_entity_ids": candidate.get("merged_entity_ids") or [],
            "reason": candidate.get("reason"),
            "candidate_key": candidate.get("candidate_key"),
        }
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
        "entity_merge": 0,
        "page_merge": 0,
        "rehome_fact": 1,
        "page_split": 2,
        "edit_contract": 3,
    }
    return (
        -float(candidate.get("score") or candidate.get("similarity") or 0.0),
        action_priority.get(str(candidate.get("action_type") or ""), 99),
        ",".join(str(path) for path in candidate.get("page_hints") or [])
        or ",".join(str(entity_id) for entity_id in candidate.get("entity_ids") or []),
    )


def candidate_key(
    action_type: str, page_hints: list[str], fact_ids: list[str] | None = None
) -> str:
    pages = ",".join(sorted(str(path) for path in page_hints if path))
    facts = ",".join(sorted(str(fact_id) for fact_id in fact_ids or [] if fact_id))
    return f"{action_type}:{pages}:{facts}"


def entity_candidate_key(action_type: str, entity_ids: list[str]) -> str:
    entities = ",".join(sorted(str(entity_id) for entity_id in entity_ids if entity_id))
    return f"{action_type}:entities:{entities}"


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
        open_placeholders = ",".join("?" for _ in OPEN_GARDENER_STATUSES)
        open_rows = conn.execute(
            f"""
            SELECT action_features, evidence_json
            FROM cos_actions
            WHERE proposed_by IN ('gardener', 'gardener_llm')
              AND status IN ({open_placeholders})
            ORDER BY created_at DESC
            """,
            OPEN_GARDENER_STATUSES,
        )
        suppressed_rows = conn.execute(
            f"""
            SELECT action_features, evidence_json
            FROM cos_actions
            WHERE proposed_by IN ('gardener', 'gardener_llm')
              AND status IN ({placeholders})
            ORDER BY created_at DESC
            LIMIT 200
            """,
            SUPPRESSED_GARDENER_STATUSES,
        )
        for row in [*open_rows, *suppressed_rows]:
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


def stable_unique(values: list[str]) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not value or value in seen:
            continue
        output.append(value)
        seen.add(value)
    return output


def stable_page_slug(page_hint: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", Path(page_hint).with_suffix("").as_posix().lower())
    return slug.strip("-")[:80] or "page"


def human_title_for_candidate(page_hint: str) -> str:
    stem = Path(page_hint).with_suffix("").name
    return stem.replace("-", " ").replace("_", " ").title()
