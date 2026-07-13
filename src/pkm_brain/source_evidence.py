from __future__ import annotations

import re
from typing import Any


DEFAULT_EVIDENCE_UNIT_TARGET_TOKENS = 80
SENTENCE_BOUNDARY_RE = re.compile(r"(?<=[.!?])\s+|\n+")
TOKEN_SPAN_RE = re.compile(r"\S+")
SPEAKER_LABEL_RE = re.compile(r"(?m)^(?P<speaker>Speaker \d+):[ \t]*")
HEADING_RE = re.compile(r"(?m)^#{1,6}\s+")
EXTRACTION_CONFIDENCE_FIELDS = (
    "extraction_confidence",
    "routing_confidence",
    "truth_confidence",
)


def extraction_confidence_values(
    item: dict[str, Any], *, require_all: bool
) -> tuple[dict[str, float | None], list[str]]:
    values: dict[str, float | None] = {}
    errors: list[str] = []
    for field in EXTRACTION_CONFIDENCE_FIELDS:
        if field not in item:
            if require_all:
                errors.append(f"missing {field}")
            legacy = item.get("confidence") if field == "truth_confidence" else None
            values[field] = float(legacy) if legacy is not None else None
            continue
        try:
            value = float(item[field])
        except (TypeError, ValueError):
            errors.append(f"{field} must be a number between 0 and 1")
            values[field] = None
            continue
        if not 0.0 <= value <= 1.0:
            errors.append(f"{field} must be between 0 and 1")
        values[field] = value
    if values["truth_confidence"] is None:
        values["truth_confidence"] = 0.5
    return values, errors


def evidence_units_for_text(
    text: str,
    *,
    target_tokens: int = DEFAULT_EVIDENCE_UNIT_TARGET_TOKENS,
) -> list[dict[str, Any]]:
    units: list[dict[str, Any]] = []
    speaker_events = list(SPEAKER_LABEL_RE.finditer(text))
    heading_events = list(HEADING_RE.finditer(text))
    for start, end in evidence_unit_segment_spans(text, target_tokens=target_tokens):
        trimmed_start, trimmed_end = trim_span_whitespace(text, start, end)
        if trimmed_start >= trimmed_end:
            continue
        unit: dict[str, Any] = {
            "unit_id": f"u{len(units)}",
            "unit_index": len(units),
            "start": trimmed_start,
            "end": trimmed_end,
            "text": text[trimmed_start:trimmed_end],
        }
        speaker = speaker_at_offset(
            trimmed_start,
            speaker_events=speaker_events,
            heading_events=heading_events,
        )
        if speaker:
            unit["speaker"] = speaker
        units.append(unit)
    return units


def evidence_unit_segment_spans(
    text: str, *, target_tokens: int
) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    start = 0
    for match in SENTENCE_BOUNDARY_RE.finditer(text):
        end = match.start()
        if start < end:
            spans.extend(
                split_long_evidence_span(text, start, end, target_tokens=target_tokens)
            )
        start = match.end()
    if start < len(text):
        spans.extend(
            split_long_evidence_span(
                text, start, len(text), target_tokens=target_tokens
            )
        )
    return spans


def split_long_evidence_span(
    text: str,
    start: int,
    end: int,
    *,
    target_tokens: int,
) -> list[tuple[int, int]]:
    token_matches = list(TOKEN_SPAN_RE.finditer(text[start:end]))
    if len(token_matches) <= target_tokens:
        return [(start, end)]
    spans: list[tuple[int, int]] = []
    for token_start in range(0, len(token_matches), target_tokens):
        window = token_matches[token_start : token_start + target_tokens]
        if not window:
            continue
        spans.append((start + window[0].start(), start + window[-1].end()))
    return spans


def resolve_evidence_unit_ids(
    text: str,
    *,
    chunk_id: str,
    unit_ids: list[str],
) -> dict[str, Any]:
    units_by_id = {unit["unit_id"]: unit for unit in evidence_units_for_text(text)}
    missing = [unit_id for unit_id in unit_ids if unit_id not in units_by_id]
    if missing:
        return {
            "source_spans": [],
            "quotes": [],
            "source_ids": [],
            "evidence_units": [],
            "missing_unit_ids": missing,
        }
    selected = sorted(
        (units_by_id[unit_id] for unit_id in unit_ids),
        key=lambda unit: unit["unit_index"],
    )
    spans: list[dict[str, Any]] = []
    quotes: list[str] = []
    for group in consecutive_unit_groups(selected):
        start = int(group[0]["start"])
        end = int(group[-1]["end"])
        spans.append({"chunk_id": chunk_id, "start": start, "end": end})
        quotes.append(text[start:end])
    evidence_units = [
        {
            "chunk_id": chunk_id,
            "unit_id": unit["unit_id"],
            "start": unit["start"],
            "end": unit["end"],
            **({"speaker": unit["speaker"]} if unit.get("speaker") else {}),
        }
        for unit in selected
    ]
    return {
        "source_spans": spans,
        "quotes": quotes,
        "source_ids": [f"chunk:{chunk_id}"],
        "evidence_units": evidence_units,
        "missing_unit_ids": [],
    }


def consecutive_unit_groups(
    units: list[dict[str, Any]],
) -> list[list[dict[str, Any]]]:
    groups: list[list[dict[str, Any]]] = []
    for unit in units:
        if (
            not groups
            or int(unit["unit_index"]) != int(groups[-1][-1]["unit_index"]) + 1
        ):
            groups.append([unit])
            continue
        groups[-1].append(unit)
    return groups


def speaker_at_offset(
    offset: int,
    *,
    speaker_events: list[re.Match[str]],
    heading_events: list[re.Match[str]],
) -> str | None:
    preceding_speaker = next(
        (match for match in reversed(speaker_events) if match.start() <= offset), None
    )
    if preceding_speaker is None:
        return None
    preceding_heading = next(
        (match for match in reversed(heading_events) if match.start() <= offset), None
    )
    if (
        preceding_heading is not None
        and preceding_heading.start() > preceding_speaker.start()
    ):
        return None
    return str(preceding_speaker.group("speaker"))


def trim_span_whitespace(text: str, start: int, end: int) -> tuple[int, int]:
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return start, end
