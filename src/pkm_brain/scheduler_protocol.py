from __future__ import annotations

import json
import math
import os
from pathlib import Path
from typing import Any


SCHEDULER_STATUS_MAX_BYTES = 64 * 1024
SCHEDULER_STATUS_MAX_DEPTH = 8
SCHEDULER_STATUS_MAX_ITEMS = 100
SCHEDULER_STATUS_MAX_STRING_CHARS = 2_048

_SENSITIVE_KEYS = {
    "api_key",
    "authorization",
    "cookie",
    "password",
    "refresh_token",
    "secret",
    "session",
    "token",
}


def sanitize_scheduler_payload(value: Any) -> dict[str, Any]:
    """Return a bounded JSON object suitable for daemon scheduler status."""

    try:
        json_value = json.loads(json.dumps(value, default=str))
    except (TypeError, ValueError):
        return {
            "status": "failed",
            "error": "scheduled job returned an invalid result",
        }
    if not isinstance(json_value, dict):
        json_value = {"result": json_value}
    sanitized = _sanitize_value(json_value, depth=SCHEDULER_STATUS_MAX_DEPTH)
    if not isinstance(sanitized, dict):  # Defensive; the root is normalized above.
        sanitized = {"result": sanitized}
    encoded = _encode(sanitized)
    if len(encoded) <= SCHEDULER_STATUS_MAX_BYTES:
        return sanitized
    status = sanitized.get("status")
    bounded: dict[str, Any] = {
        "status": (
            _truncate_string(str(status))
            if status is not None
            else ("skipped" if sanitized.get("skipped") else "success")
        ),
        "truncated": True,
        "message": "scheduled job result exceeded the status size limit",
    }
    for key in ("reason", "error", "error_type"):
        if key in sanitized:
            bounded[key] = _truncate_string(str(sanitized[key]))
    return bounded


def write_scheduler_status(path: Path, value: Any) -> None:
    """Write a private status document without ever exceeding the protocol cap."""

    payload = sanitize_scheduler_payload(value)
    encoded = _encode(payload)
    if len(encoded) > SCHEDULER_STATUS_MAX_BYTES:  # pragma: no cover - invariant guard
        payload = {
            "status": "failed",
            "error": "scheduled job status could not be bounded",
        }
        encoded = _encode(payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
    except Exception:
        os.close(descriptor)
        raise
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(encoded)
        handle.write(b"\n")


def read_scheduler_status(path: Path) -> dict[str, Any]:
    """Read one child result while refusing oversized or malformed documents."""

    try:
        with path.open("rb") as handle:
            encoded = handle.read(SCHEDULER_STATUS_MAX_BYTES + 1)
    except OSError:
        return {
            "status": "failed",
            "error": "scheduled child exited without a status result",
        }
    if len(encoded) > SCHEDULER_STATUS_MAX_BYTES:
        return {
            "status": "failed",
            "error": "scheduled child returned an oversized status result",
        }
    try:
        payload = json.loads(encoded)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {
            "status": "failed",
            "error": "scheduled child returned an invalid status result",
        }
    if not isinstance(payload, dict):
        return {
            "status": "failed",
            "error": "scheduled child returned a non-object status result",
        }
    return sanitize_scheduler_payload(payload)


def _sanitize_value(value: Any, *, depth: int) -> Any:
    if depth <= 0:
        return "[truncated]"
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        items = list(value.items())
        for raw_key, nested in items[:SCHEDULER_STATUS_MAX_ITEMS]:
            key = _truncate_string(str(raw_key), limit=128)
            if _is_sensitive_key(key):
                sanitized[key] = "[redacted]"
            else:
                sanitized[key] = _sanitize_value(nested, depth=depth - 1)
        if len(items) > SCHEDULER_STATUS_MAX_ITEMS:
            sanitized["_truncated_items"] = len(items) - SCHEDULER_STATUS_MAX_ITEMS
        return sanitized
    if isinstance(value, list):
        sanitized_list = [
            _sanitize_value(item, depth=depth - 1)
            for item in value[:SCHEDULER_STATUS_MAX_ITEMS]
        ]
        if len(value) > SCHEDULER_STATUS_MAX_ITEMS:
            sanitized_list.append(
                {"_truncated_items": len(value) - SCHEDULER_STATUS_MAX_ITEMS}
            )
        return sanitized_list
    if isinstance(value, str):
        return _truncate_string(value)
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _truncate_string(str(value))


def _is_sensitive_key(key: str) -> bool:
    normalized = key.lower().replace("-", "_").replace(" ", "_")
    return (
        normalized in _SENSITIVE_KEYS
        or normalized.endswith("_api_key")
        or normalized.endswith("_password")
        or normalized.endswith("_secret")
        or normalized.endswith("_token")
    )


def _truncate_string(value: str, *, limit: int = SCHEDULER_STATUS_MAX_STRING_CHARS) -> str:
    if len(value) <= limit:
        return value
    return value[:limit] + "…[truncated]"


def _encode(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
