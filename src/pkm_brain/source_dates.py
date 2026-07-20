from __future__ import annotations

import re
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from .chunking import strip_frontmatter
from .gmail_projection import gmail_projection_session_id
from .util import file_sha256, slugify

SOURCE_DATE_FIELDS = (
    ("event_started_at", "source_event_started_at"),
    ("source_created_at", "source_created_at"),
    ("captured_at", "source_captured_at"),
    ("document_created_at", "source_created_at"),
    ("ingested_at", "source_ingested_at"),
)
GMAIL_MESSAGE_TIMESTAMPS_VERSION = 1
GMAIL_SOURCE_REVISION = re.compile(r"^[0-9a-f]{64}$")
GMAIL_PROVIDER_MESSAGE_ID = re.compile(r"^[A-Za-z0-9_.-]+$")
SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")


def document_source_date_metadata(document: dict[str, Any]) -> dict[str, Any]:
    frontmatter, frontmatter_path = source_frontmatter_with_path(document)
    gmail_message_timestamps = trusted_gmail_message_timestamps(
        document,
        frontmatter,
        frontmatter_path,
    )
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
        "structured_gmail_message_metadata_trusted": (
            gmail_message_timestamps is not None
        ),
        # This connector-authored index is deliberately kept on the internal
        # document card. source_window_card does not expose it to the extractor.
        "trusted_gmail_message_timestamps": gmail_message_timestamps or [],
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
    observed_at = text_value(fact.get("observed_at"))
    metadata = fact.get("metadata")
    observed_at_basis = (
        text_value(metadata.get("observed_at_basis"))
        if isinstance(metadata, dict)
        else ""
    )
    if observed_at and observed_at_basis not in {
        "",
        "unknown",
        "gmail_message_time_unresolved",
    }:
        # Extraction stamps the assertion clock and its provenance together.  Keep
        # that pairing when an exact duplicate later gains additional sources; a
        # Gmail source in the union does not retroactively make a non-Gmail clock a
        # provider internalDate.
        return observed_at, observed_at_basis
    gmail_documents = [
        document
        for document in documents
        if str(document.get("source_type") or "") == "gmail_thread"
    ]
    if observed_at and gmail_documents and len(gmail_documents) == len(documents):
        # A Gmail thread's document date is its first message. The cited provider
        # clock is the assertion date for a fact from any later message.  This is a
        # legacy fallback for facts persisted before observed_at_basis was stamped;
        # mixed-source facts require explicit provenance instead.
        return observed_at, "gmail_internal_date"
    source_dates = [
        (text_value(document.get("source_date")), document.get("source_date_basis"))
        for document in documents
        if text_value(document.get("source_date"))
    ]
    if source_dates:
        value, basis = max(source_dates, key=lambda item: date_sort_key(item[0]))
        return value, text_value(basis) or "source_document"
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
    gmail_message = gmail_candidate_message_context(candidate, document)
    if gmail_message is not None:
        candidate["observed_at"] = gmail_message[0]
        metadata["observed_at_basis"] = "gmail_internal_date"
        metadata["gmail_message_id"] = gmail_message[1]
    elif str(document.get("source_type") or "") == "gmail_thread":
        candidate["observed_at"] = None
        metadata["observed_at_basis"] = "gmail_message_time_unresolved"
        metadata.pop("gmail_message_id", None)
    elif document.get("source_created_at"):
        candidate["observed_at"] = document["source_created_at"]
        metadata["observed_at_basis"] = "source_created_at"
    else:
        candidate["observed_at"] = None
        metadata["observed_at_basis"] = "unknown"
    candidate["metadata"] = metadata


def gmail_candidate_message_context(
    candidate: dict[str, Any], document: dict[str, Any]
) -> tuple[str, str] | None:
    if str(document.get("source_type") or "") != "gmail_thread":
        return None
    timestamp_index = document.get("trusted_gmail_message_timestamps")
    if not isinstance(timestamp_index, list) or not timestamp_index:
        return None
    chunks = {
        str(chunk.get("chunk_id") or ""): chunk
        for chunk in document.get("chunks") or []
        if isinstance(chunk, dict) and str(chunk.get("chunk_id") or "")
    }
    spans = candidate.get("source_spans")
    if not isinstance(spans, list) or not spans:
        return None

    resolved_messages: set[tuple[str, str]] = set()
    for span in spans:
        if not isinstance(span, dict):
            return None
        chunk = chunks.get(str(span.get("chunk_id") or ""))
        if chunk is None:
            return None
        relative_start = strict_int(span.get("start"))
        relative_end = strict_int(span.get("end"))
        chunk_start = strict_int(chunk.get("start_offset"))
        chunk_end = strict_int(chunk.get("end_offset"))
        chunk_text = str(chunk.get("text") or "")
        if (
            relative_start is None
            or relative_end is None
            or chunk_start is None
            or chunk_end is None
            or relative_start < 0
            or relative_end <= relative_start
            or relative_end > len(chunk_text)
            or chunk_start < 0
            or chunk_end <= chunk_start
            or chunk_end < chunk_start + len(chunk_text)
        ):
            return None
        absolute_start = chunk_start + relative_start
        absolute_end = chunk_start + relative_end
        matching_messages = [
            item
            for item in timestamp_index
            if isinstance(item, dict)
            and strict_int(item.get("start_offset")) is not None
            and strict_int(item.get("end_offset")) is not None
            and int(item["start_offset"]) <= absolute_start
            and absolute_end <= int(item["end_offset"])
        ]
        if len(matching_messages) != 1:
            return None
        message = matching_messages[0]
        message_id = text_value(message.get("message_id"))
        internal_date = text_value(message.get("internal_date"))
        if not message_id or not internal_date:
            return None
        resolved_messages.add((message_id, internal_date))

    if len(resolved_messages) != 1:
        return None
    message_id, internal_date = resolved_messages.pop()
    parsed = aware_datetime(internal_date)
    if parsed is None:
        return None
    return parsed.astimezone(timezone.utc).isoformat(), message_id


def trusted_gmail_message_timestamps(
    document: dict[str, Any],
    frontmatter: dict[str, Any],
    frontmatter_path: Path | None,
) -> list[dict[str, Any]] | None:
    """Validate the connector-authored provider timestamp/range index.

    Message headings and message bodies are untrusted email content.  A timestamp
    becomes an assertion clock only when the immutable ingested file, connector
    lineage, provider message id, provider internal date, and rendered range all
    agree.
    """

    if str(document.get("source_type") or "") != "gmail_thread":
        return None
    if text_value(frontmatter.get("source_type")) != "gmail_thread":
        return None
    if strict_int(frontmatter.get("gmail_message_timestamps_version")) != (
        GMAIL_MESSAGE_TIMESTAMPS_VERSION
    ):
        return None
    account_key = text_value(frontmatter.get("gmail_account_key"))
    thread_id = text_value(frontmatter.get("gmail_thread_id"))
    source_revision = text_value(frontmatter.get("gmail_source_revision"))
    if (
        not account_key
        or not thread_id
        or not GMAIL_SOURCE_REVISION.fullmatch(source_revision)
        or frontmatter_path is None
    ):
        return None
    raw_projection_version = frontmatter.get("gmail_projection_version")
    if raw_projection_version is None:
        # Preserve trust for immutable evidence emitted before explicit projection
        # versioning. New renderers always use the collision-resistant identity.
        expected_stem = slugify(
            f"{account_key}--{thread_id}--{source_revision[:20]}"
        )
    else:
        projection_version = strict_int(raw_projection_version)
        if projection_version is None or projection_version < 1:
            return None
        expected_stem = slugify(
            gmail_projection_session_id(
                account_key=account_key,
                thread_id=thread_id,
                source_revision=source_revision,
                projection_version=projection_version,
            )
        )
    if not trusted_gmail_document_paths(document, frontmatter_path, expected_stem):
        return None
    content_hash = text_value(document.get("content_hash"))
    if not SHA256_HEX.fullmatch(content_hash):
        return None
    try:
        actual_content_hash = file_sha256(frontmatter_path)
    except OSError:
        return None
    if actual_content_hash != content_hash:
        return None

    message_ids = frontmatter.get("gmail_message_ids")
    entries = frontmatter.get("gmail_message_timestamps")
    if not isinstance(message_ids, list) or not isinstance(entries, list):
        return None
    if any(
        not isinstance(message_id, str)
        or not GMAIL_PROVIDER_MESSAGE_ID.fullmatch(message_id)
        for message_id in message_ids
    ):
        return None
    if len(set(message_ids)) != len(message_ids) or len(entries) != len(message_ids):
        return None
    retained_count = strict_int(frontmatter.get("retained_message_count"))
    if retained_count is None or retained_count != len(message_ids):
        return None

    try:
        source_text = frontmatter_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    body = strip_frontmatter(source_text)
    normalized: list[dict[str, Any]] = []
    previous_end = -1
    expected_keys = {"message_id", "internal_date", "start_offset", "end_offset"}
    for index, (message_id, raw_entry) in enumerate(
        zip(message_ids, entries), start=1
    ):
        if not isinstance(raw_entry, dict) or set(raw_entry) != expected_keys:
            return None
        if raw_entry.get("message_id") != message_id:
            return None
        internal_date = raw_entry.get("internal_date")
        if not isinstance(internal_date, str):
            return None
        if internal_date and aware_datetime(internal_date) is None:
            return None
        start_offset = strict_int(raw_entry.get("start_offset"))
        end_offset = strict_int(raw_entry.get("end_offset"))
        if (
            start_offset is None
            or end_offset is None
            or start_offset < 0
            or end_offset <= start_offset
            or end_offset > len(body)
            or start_offset <= previous_end
        ):
            return None
        if index == 1:
            if not body.startswith("# Email thread:") or not body[:start_offset].endswith(
                "\n\n"
            ):
                return None
        elif body[previous_end:start_offset] != "\n\n":
            return None
        first_line = body[start_offset:end_offset].split("\n", 1)[0]
        if internal_date:
            expected_heading = (
                f"## Message {index} — {internal_date} — {message_id}"
            )
            if first_line != expected_heading:
                return None
        elif not (
            first_line.startswith(f"## Message {index} — ")
            and first_line.endswith(f" — {message_id}")
        ):
            return None
        normalized.append(
            {
                "message_id": message_id,
                "internal_date": internal_date,
                "start_offset": start_offset,
                "end_offset": end_offset,
            }
        )
        previous_end = end_offset
    if normalized and previous_end != len(body):
        return None
    return normalized


def trusted_gmail_document_paths(
    document: dict[str, Any], selected_path: Path, expected_stem: str
) -> bool:
    source_paths = stable_paths(document.get("source_path"))
    if not any(
        path.suffix.casefold() == ".md"
        and path.stem == expected_stem
        and path.parent.name == "gmail"
        and path.parent.parent.name == "documents"
        and path.parent.parent.parent.name == "inbox"
        for path in source_paths
    ):
        return False
    if selected_path in source_paths:
        return True
    raw_paths = stable_paths(document.get("raw_path"))
    content_hash = text_value(document.get("content_hash"))
    expected_raw_stem = f"{expected_stem}-{content_hash[:12]}"
    return any(
        path == selected_path
        and path.suffix.casefold() == ".md"
        and path.stem == expected_raw_stem
        and path.parent.parent.parent.name == "gmail_thread"
        and path.parent.parent.parent.parent.name == "raw"
        for path in raw_paths
    )


def aware_datetime(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def strict_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def source_frontmatter(document: dict[str, Any]) -> dict[str, Any]:
    return source_frontmatter_with_path(document)[0]


def source_frontmatter_with_path(
    document: dict[str, Any],
) -> tuple[dict[str, Any], Path | None]:
    for path in stable_paths(document.get("raw_path"), document.get("source_path")):
        parsed = read_frontmatter(path)
        if parsed:
            return parsed, path
    return {}, None


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
