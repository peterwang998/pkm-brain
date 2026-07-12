from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .wiki_facts import fact_tokens, facts_directly_conflict, facts_should_merge


RELATION_VALUES = {
    "duplicate",
    "supports",
    "refines",
    "updates",
    "complementary",
    "contradicts",
    "unrelated",
}

AUTO_COMPATIBLE_RELATIONS = {
    "duplicate",
    "supports",
    "refines",
    "updates",
    "complementary",
    "unrelated",
}

CONTRADICTION_RECALL_THRESHOLD = 0.9
FALSE_CONFLICT_RATE_THRESHOLD = 0.1

TOKEN_RE = re.compile(r"[a-z0-9]+")
DATE_RE = re.compile(
    r"\b(?:20\d{2}|19\d{2}|\d{4}-\d{2}-\d{2}|jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\b",
    re.IGNORECASE,
)
VALUE_RE = re.compile(r"(?<![A-Za-z0-9])(?:\$?\d+(?:[.,]\d+)?%?|\d+(?:st|nd|rd|th))(?![A-Za-z0-9])")
PROGRESSION_WORDS = {
    "became",
    "becomes",
    "current",
    "currently",
    "earlier",
    "final",
    "formerly",
    "later",
    "moved",
    "new",
    "now",
    "previous",
    "previously",
    "then",
    "updated",
}
NEGATION_WORDS = {"not", "no", "never", "without"}
CLAIM_LOW_SIGNAL_TOKENS = {
    "also",
    "believ",
    "expect",
    "he",
    "mention",
    "say",
    "said",
    "she",
    "think",
    "they",
    "while",
}


@dataclass(frozen=True)
class FactRelation:
    relation: str
    confidence: float
    rationale: str
    compatible: bool
    classifier_version: str = "deterministic-v2"
    existing_fact_id: str | None = None
    candidate_fact_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "relation": self.relation,
            "confidence": round(self.confidence, 3),
            "rationale": self.rationale,
            "compatible": self.compatible,
            "classifier_version": self.classifier_version,
            "existing_fact_id": self.existing_fact_id,
            "candidate_fact_id": self.candidate_fact_id,
        }


def classify_fact_relation(
    candidate: dict[str, Any],
    existing: dict[str, Any],
) -> FactRelation:
    candidate_statement = str(candidate.get("statement") or "")
    existing_statement = str(existing.get("statement") or "")
    candidate_id = optional_id(candidate)
    existing_id = optional_id(existing)
    if not candidate_statement or not existing_statement:
        return fact_relation(
            "unrelated",
            0.2,
            "one side is missing a statement",
            candidate_id=candidate_id,
            existing_id=existing_id,
        )

    same_entity = facts_share_entity(candidate, existing)
    candidate_tokens = statement_tokens(candidate_statement)
    existing_tokens = statement_tokens(existing_statement)
    overlap = token_overlap(candidate_tokens, existing_tokens)
    jaccard = token_jaccard(candidate_tokens, existing_tokens)
    negation_conflict = asymmetric_negation(candidate_tokens, existing_tokens)
    # Material words, numbers, or negation are only candidate-pair signals. A
    # contradiction verdict also requires the statements to share claim-level
    # anchors; otherwise two attributes on a broad entity become false conflicts.
    contradiction_signal = facts_directly_conflict(
        existing, candidate
    ) and claims_overlap_for_verdict(candidate_statement, existing_statement)

    if normalize_statement(candidate_statement) == normalize_statement(existing_statement):
        if disjoint_sources(candidate, existing):
            return fact_relation(
                "supports",
                0.94,
                "same claim with additional source evidence",
                candidate_id=candidate_id,
                existing_id=existing_id,
            )
        return fact_relation(
            "duplicate",
            0.96,
            "same claim as an existing fact",
            candidate_id=candidate_id,
            existing_id=existing_id,
        )

    if same_entity and has_progression_language(candidate_statement, existing_statement) and overlap >= 0.2:
        return fact_relation(
            "complementary",
            0.7,
            "candidate and existing fact read as compatible temporal progression",
            candidate_id=candidate_id,
            existing_id=existing_id,
        )

    if contradiction_signal and same_entity:
        if negation_conflict:
            return fact_relation(
                "contradicts",
                0.9,
                "same entity with asymmetric negation",
                candidate_id=candidate_id,
                existing_id=existing_id,
            )
        if explicitly_dated(candidate) and explicitly_dated(existing) and overlap >= 0.35:
            return fact_relation(
                "updates",
                0.72,
                "same entity with dated succession signal; keep deterministic apply for later gate",
                candidate_id=candidate_id,
                existing_id=existing_id,
            )
        if has_progression_language(candidate_statement, existing_statement) and overlap >= 0.25:
            return fact_relation(
                "complementary",
                0.66,
                "lexical conflict signal is explainable as temporal progression or refinement",
                candidate_id=candidate_id,
                existing_id=existing_id,
            )
        return fact_relation(
            "contradicts",
            0.82,
            "same entity with a direct contradiction signal",
            candidate_id=candidate_id,
            existing_id=existing_id,
        )

    if same_entity and statement_refines(candidate_statement, existing_statement, candidate_tokens, existing_tokens):
        return fact_relation(
            "refines",
            0.74,
            "candidate adds detail to the same claim",
            candidate_id=candidate_id,
            existing_id=existing_id,
        )

    if facts_should_merge(existing, candidate):
        relation = "supports" if disjoint_sources(candidate, existing) else "duplicate"
        return fact_relation(
            relation,
            0.9,
            "near-duplicate claim that can merge provenance",
            candidate_id=candidate_id,
            existing_id=existing_id,
        )

    if same_entity and overlap >= 0.72:
        relation = "supports" if disjoint_sources(candidate, existing) else "duplicate"
        return fact_relation(
            relation,
            0.82,
            "high statement overlap on the same entity",
            candidate_id=candidate_id,
            existing_id=existing_id,
        )

    if same_entity and (overlap >= 0.2 or jaccard >= 0.12):
        return fact_relation(
            "complementary",
            0.68,
            "same entity with compatible different attributes",
            candidate_id=candidate_id,
            existing_id=existing_id,
        )

    return fact_relation(
        "unrelated",
        0.72,
        "insufficient shared entity or claim overlap",
        candidate_id=candidate_id,
        existing_id=existing_id,
    )


def fact_relation(
    relation: str,
    confidence: float,
    rationale: str,
    *,
    candidate_id: str | None,
    existing_id: str | None,
) -> FactRelation:
    normalized = relation if relation in RELATION_VALUES else "unrelated"
    return FactRelation(
        relation=normalized,
        confidence=max(0.0, min(1.0, confidence)),
        rationale=rationale,
        compatible=normalized in AUTO_COMPATIBLE_RELATIONS,
        existing_fact_id=existing_id,
        candidate_fact_id=candidate_id,
    )


def optional_id(fact: dict[str, Any]) -> str | None:
    value = str(fact.get("id") or fact.get("fact_id") or "").strip()
    return value or None


def facts_share_entity(candidate: dict[str, Any], existing: dict[str, Any]) -> bool:
    candidate_entity_id = str(candidate.get("entity_id") or "").strip()
    existing_entity_id = str(existing.get("entity_id") or "").strip()
    if candidate_entity_id and existing_entity_id:
        return candidate_entity_id == existing_entity_id
    compared = False
    for key in ("entity_key", "page_hint"):
        left = str(candidate.get(key) or "").strip().lower()
        right = str(existing.get(key) or "").strip().lower()
        if left and right:
            compared = True
        if left and right and left == right:
            return True
    return not compared


def normalize_statement(value: str) -> str:
    return " ".join(statement_tokens(value))


def statement_tokens(value: str) -> list[str]:
    return TOKEN_RE.findall(value.lower())


def token_overlap(left: list[str], right: list[str]) -> float:
    left_set = set(left)
    right_set = set(right)
    if not left_set or not right_set:
        return 0.0
    return len(left_set & right_set) / min(len(left_set), len(right_set))


def token_jaccard(left: list[str], right: list[str]) -> float:
    left_set = set(left)
    right_set = set(right)
    union = left_set | right_set
    if not union:
        return 0.0
    return len(left_set & right_set) / len(union)


def claims_overlap_for_verdict(left: str, right: str) -> bool:
    left_tokens = set(fact_tokens(left)) - CLAIM_LOW_SIGNAL_TOKENS
    right_tokens = set(fact_tokens(right)) - CLAIM_LOW_SIGNAL_TOKENS
    if not left_tokens or not right_tokens:
        return False
    shared = left_tokens & right_tokens
    overlap = len(shared) / min(len(left_tokens), len(right_tokens))
    jaccard = len(shared) / len(left_tokens | right_tokens)
    return overlap >= 0.35 and jaccard >= 0.25


def disjoint_sources(candidate: dict[str, Any], existing: dict[str, Any]) -> bool:
    candidate_sources = source_id_set(candidate.get("source_ids"))
    existing_sources = source_id_set(existing.get("source_ids"))
    return bool(candidate_sources and existing_sources and not candidate_sources & existing_sources)


def source_id_set(value: Any) -> set[str]:
    if isinstance(value, str):
        try:
            import json

            parsed = json.loads(value)
        except json.JSONDecodeError:
            parsed = [value]
        value = parsed
    if not isinstance(value, list):
        return set()
    return {str(item).strip() for item in value if str(item).strip()}


def explicitly_dated(fact: dict[str, Any]) -> bool:
    for key in ("effective_at", "observed_at", "created_at"):
        if DATE_RE.search(str(fact.get(key) or "")):
            return True
    return DATE_RE.search(str(fact.get("statement") or "")) is not None


def has_progression_language(*statements: str) -> bool:
    tokens: set[str] = set()
    for statement in statements:
        tokens.update(statement_tokens(statement))
    return bool(tokens & PROGRESSION_WORDS)


def asymmetric_negation(left: list[str], right: list[str]) -> bool:
    return bool(set(left) & NEGATION_WORDS) != bool(set(right) & NEGATION_WORDS)


def statement_refines(
    candidate_statement: str,
    existing_statement: str,
    candidate_tokens: list[str],
    existing_tokens: list[str],
) -> bool:
    candidate_norm = normalize_statement(candidate_statement)
    existing_norm = normalize_statement(existing_statement)
    if existing_norm and existing_norm in candidate_norm and len(candidate_tokens) > len(existing_tokens):
        return True
    existing_values = set(VALUE_RE.findall(existing_statement))
    candidate_values = set(VALUE_RE.findall(candidate_statement))
    if existing_values and candidate_values and existing_values != candidate_values:
        return False
    return token_overlap(candidate_tokens, existing_tokens) >= 0.45 and len(candidate_tokens) > len(existing_tokens)
