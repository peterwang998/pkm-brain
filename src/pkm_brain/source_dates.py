from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import yaml

SOURCE_DATE_FIELDS = (
    ("event_started_at", "source_event_started_at"),
    ("source_created_at", "source_created_at"),
    ("captured_at", "source_captured_at"),
    ("document_created_at", "source_created_at"),
    ("ingested_at", "source_ingested_at"),
)


def document_source_date_metadata(document: dict[str, Any]) -> dict[str, Any]:
    frontmatter = source_frontmatter(document)
    values = {
        "event_started_at": text_value(frontmatter.get("event_started_at")),
        "event_ended_at": text_value(frontmatter.get("event_ended_at")),
        "event_session_id": text_value(frontmatter.get("session_id")),
        "source_updated_at": text_value(frontmatter.get("source_updated_at")),
        "source_created_at": text_value(frontmatter.get("created_at")),
        "captured_at": text_value(frontmatter.get("captured_at")),
        "document_created_at": text_value(document.get("created_at")),
        "ingested_at": text_value(document.get("ingested_at")),
    }
    source_date = None
    source_date_basis = None
    for field, basis in SOURCE_DATE_FIELDS:
        if values[field]:
            source_date = values[field]
            source_date_basis = basis
            break
    return {
        **values,
        "structured_event_metadata_trusted": trusted_hyprnote_event_metadata(
            document, frontmatter
        ),
        "source_date": source_date,
        "source_date_basis": source_date_basis,
    }


def trusted_hyprnote_event_metadata(
    document: dict[str, Any], frontmatter: dict[str, Any]
) -> bool:
    """Recognize captured Hyprnote metadata, not a self-declared source label alone."""

    if str(document.get("source_type") or "") != "hyprnote_meeting":
        return False
    if text_value(frontmatter.get("source_type")) != "hyprnote_meeting":
        return False
    if text_value(frontmatter.get("agent")).casefold() != "hyprnote":
        return False
    session_id = text_value(frontmatter.get("session_id"))
    if not session_id:
        return False
    captured_path_matches = any(
        path.suffix.casefold() == ".md"
        and path.stem == session_id
        and path.parent.name == "hyprnote"
        and path.parent.parent.name == "documents"
        for path in stable_paths(document.get("source_path"), document.get("raw_path"))
    )
    if not captured_path_matches:
        return False
    original_path = Path(text_value(frontmatter.get("source_path"))).expanduser()
    return (
        original_path.name == session_id
        and original_path.parent.name == "sessions"
        and original_path.parent.parent.name == "hyprnote"
    )


def derive_fact_source_date(
    fact: dict[str, Any], documents: list[dict[str, Any]]
) -> tuple[str | None, str | None]:
    source_dates = [
        (text_value(document.get("source_date")), document.get("source_date_basis"))
        for document in documents
        if text_value(document.get("source_date"))
    ]
    if source_dates:
        value, basis = max(source_dates, key=lambda item: date_sort_key(item[0]))
        return value, text_value(basis) or "source_document"
    observed_at = text_value(fact.get("observed_at"))
    if observed_at:
        return observed_at, "observed_at"
    return None, None


def stamp_candidate_source_context(
    candidate: dict[str, Any], document: dict[str, Any], window_id: str
) -> None:
    metadata = (
        dict(candidate.get("metadata"))
        if isinstance(candidate.get("metadata"), dict)
        else {}
    )
    metadata["document_id"] = document["document_id"]
    metadata["window_id"] = window_id
    if document.get("source_created_at"):
        candidate["observed_at"] = document["source_created_at"]
        metadata["observed_at_basis"] = "source_created_at"
    else:
        candidate["observed_at"] = None
        metadata["observed_at_basis"] = "unknown"
    candidate["metadata"] = metadata


def source_frontmatter(document: dict[str, Any]) -> dict[str, Any]:
    for path in stable_paths(document.get("raw_path"), document.get("source_path")):
        parsed = read_frontmatter(path)
        if parsed:
            return parsed
    return {}


def read_frontmatter(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            if handle.readline().strip() != "---":
                return {}
            lines = []
            for index, line in enumerate(handle):
                if line.strip() == "---":
                    parsed = yaml.safe_load("".join(lines)) or {}
                    return parsed if isinstance(parsed, dict) else {}
                if index >= 511:
                    return {}
                lines.append(line)
    except (OSError, UnicodeError, yaml.YAMLError):
        return {}
    return {}


def stable_paths(*values: Any) -> list[Path]:
    paths = []
    for value in values:
        raw = text_value(value)
        if not raw:
            continue
        path = Path(raw).expanduser()
        if path not in paths:
            paths.append(path)
    return paths


def text_value(value: Any) -> str:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value or "").strip()


def date_sort_key(value: str) -> tuple[int, float, str]:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return 1, parsed.timestamp(), value
    except (ValueError, OverflowError):
        return 0, 0.0, value
