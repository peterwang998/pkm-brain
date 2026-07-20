from __future__ import annotations

from typing import Any

from .extraction_entities import stable_unique_strings
from .gmail_sensitive_data import (
    gmail_sensitive_values,
    sanitize_gmail_evidence_quotes,
)
from .numeric_faithfulness import unsupported_statement_numbers
from .source_evidence import resolve_evidence_unit_ids


MAX_EVIDENCE_UNITS_PER_FACT = 5


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
        return empty_resolved_evidence(
            [f"unknown chunk_id: {_clip_text(chunk_id, 80)}"]
        )
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
                f"unknown evidence_unit_id for {_clip_text(chunk_id, 80)}: "
                f"{', '.join(missing)}"
            ]
        )
    source_type = str(chunk_context.get("source_type") or "")
    quotes = list(resolved["quotes"])
    evidence_sanitization: dict[str, Any] | None = None
    if source_type == "gmail_thread":
        quotes, evidence_sanitization = sanitize_gmail_evidence_quotes(
            quotes,
            source_values=gmail_sensitive_values(text),
        )
    return {
        "source_spans": resolved["source_spans"],
        "quotes": quotes,
        "source_ids": resolved["source_ids"],
        "evidence_units": resolved["evidence_units"],
        "reasons": [],
        "source_type": source_type,
        "evidence_sanitization": evidence_sanitization,
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


def failed_facts_from_response(
    previous_response: dict[str, Any],
    validation_report: dict[str, Any],
) -> list[Any]:
    facts = previous_response.get("facts")
    if not isinstance(facts, list):
        return []
    failed = []
    for rejection in validation_report.get("rejections") or []:
        if "sensitive_gmail_credential_fact" in (rejection.get("reasons") or []):
            continue
        index = rejection.get("index")
        if isinstance(index, int) and 0 <= index < len(facts):
            failed.append(facts[index])
    return failed


def _clip_text(value: str, limit: int = 160) -> str:
    return value if len(value) <= limit else f"{value[: limit - 3]}..."
