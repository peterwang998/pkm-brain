from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from .wiki import parse_frontmatter


GMAIL_TAG_PREFIX = "gmail:"
GMAIL_BULK_RETRIEVAL_TERMS = {
    "ad",
    "ads",
    "advertisement",
    "advertisements",
    "coupon",
    "coupons",
    "newsletter",
    "newsletters",
    "promotion",
    "promotions",
    "promotional",
    "unsubscribe",
}
GMAIL_ROUTINE_RETRIEVAL_TERMS = {
    "receipt",
    "receipts",
    "shipment",
    "shipments",
}
_MAILBOX_RETRIEVAL_INTENT = re.compile(
    r"\b(?:browse|check|find|list|look|open|query|read|review|scan|search|show|"
    r"summarize|triage)\b"
    r"(?:[\s,:-]+(?:all|an?|for|in|me|my|recent|the|through|unread)){0,3}"
    r"[\s,:-]+(?:email|emails|gmail|inbox|mail|mailbox|message|messages)\b",
    re.IGNORECASE,
)
_MAILBOX_RELATION_INTENT = re.compile(
    r"\b(?:email|emails|mail|message|messages)\s+"
    r"(?:about|from|regarding|sent\s+by|to)\b",
    re.IGNORECASE,
)


def gmail_document_tags(text: str, source_type: str) -> list[str]:
    if source_type != "gmail_thread":
        return []
    frontmatter, _body = parse_frontmatter(text)
    if not isinstance(frontmatter, dict):
        return ["gmail:unclassified"]
    tags: list[str] = []
    for key, prefix in (
        ("delivery_kind", "delivery"),
        ("fact_importance", "importance"),
        ("actionability", "actionability"),
    ):
        value = normalized_tag_value(frontmatter.get(key))
        if value:
            tags.append(f"{GMAIL_TAG_PREFIX}{prefix}:{value}")
    if truthy(frontmatter.get("fact_eligible")):
        tags.append("gmail:fact-eligible")
    if truthy(frontmatter.get("deleted")):
        tags.append("gmail:deleted")
    return tags or ["gmail:unclassified"]


def gmail_retrieval_noise_reasons(
    candidate: dict[str, Any], query: str
) -> list[str]:
    if str(candidate.get("source_type") or "") != "gmail_thread":
        return []
    tags = document_tags(candidate.get("tags"))
    bulk = "gmail:delivery:bulk" in tags
    advertising = "gmail:importance:advertising" in tags
    routine_low_signal = (
        "gmail:importance:routine" in tags
        and bool(
            tags
            & {
                "gmail:delivery:transactional",
                "gmail:delivery:unknown",
            }
        )
    )
    if not (bulk or advertising or routine_low_signal):
        return []
    query_terms = {
        term.strip(".,:;!?()[]{}\"'").casefold() for term in query.split() if term.strip()
    }
    explicit_bulk_request = bool(query_terms & GMAIL_BULK_RETRIEVAL_TERMS)
    if bulk or advertising:
        if explicit_bulk_request:
            return []
        return ["bulk or advertising Gmail thread"]
    explicit_routine_request = bool(query_terms & GMAIL_ROUTINE_RETRIEVAL_TERMS)
    explicit_mailbox_search = bool(
        _MAILBOX_RETRIEVAL_INTENT.search(query)
        or _MAILBOX_RELATION_INTENT.search(query)
    )
    if routine_low_signal and not (
        explicit_bulk_request or explicit_routine_request or explicit_mailbox_search
    ):
        return ["routine low-signal Gmail thread"]
    return []


def secure_gmail_raw_directories(
    target_dir: Path, raw_root: Path, source_type: str
) -> None:
    if source_type != "gmail_thread":
        return
    current = target_dir
    while current != raw_root and raw_root in current.parents:
        os.chmod(current, 0o700)
        current = current.parent


def document_tags(value: Any) -> set[str]:
    if isinstance(value, list):
        return {str(item) for item in value if str(item)}
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            parsed = []
        if isinstance(parsed, list):
            return {str(item) for item in parsed if str(item)}
    return set()


def normalized_tag_value(value: Any) -> str:
    return "-".join(str(value or "").strip().casefold().replace("_", "-").split())


def truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().casefold() in {"1", "true", "yes", "on"}
