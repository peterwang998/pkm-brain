from __future__ import annotations

import hashlib
import json


# The pre-versioned Gmail renderer is treated as implicit v1. Explicit v2 added
# immutable renderer identities; v3 tightens the temporal admission boundary after
# the full-corpus precision audit. V4 makes admission operate on retained text only
# and makes artifact bytes deterministic. V5 scopes advertising and body-sufficiency
# decisions to the individual message ranges that fact extraction can actually see.
# V6 keeps those message scopes independent when one thread contains both a bulk
# promotion and a separate qualifying transactional event. V7 adds a trusted
# per-message delivery/advertising/relevance index so temporal review never uses
# one thread-level label as the recall boundary for a different message.
# Each semantic renderer change gets a new version.
GMAIL_KNOWLEDGE_PROJECTION_VERSION = 7
GMAIL_KNOWLEDGE_CLASSIFIER_VERSION = 5
GMAIL_MESSAGE_POLICY_VERSION = 1


def require_gmail_projection_version(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("Gmail projection version must be a positive integer")
    return value


def gmail_projection_session_id(
    *,
    account_key: str,
    thread_id: str,
    source_revision: str,
    projection_version: int = GMAIL_KNOWLEDGE_PROJECTION_VERSION,
) -> str:
    """Return a collision-resistant identity for one immutable projection."""

    version = require_gmail_projection_version(projection_version)
    identity = json.dumps(
        {
            "account_key": account_key,
            "projection_version": version,
            "source_revision": source_revision,
            "thread_id": thread_id,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    digest = hashlib.sha256(identity).hexdigest()
    return f"gmail-thread-p{version}-{digest}"
