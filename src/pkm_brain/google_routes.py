from __future__ import annotations

import re
import urllib.parse


_CONNECTOR_IDS = {"calendar", "gmail"}
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def gmail_thread_route(account: str, thread_id: str) -> str:
    safe_account = _route_component(account, "Google account", 320)
    safe_thread = _route_component(thread_id, "Gmail thread ID", 2_000)
    return (
        "https://mail.google.com/mail/u/"
        f"{urllib.parse.quote(safe_account, safe='@._+-')}/#all/"
        f"{urllib.parse.quote(safe_thread, safe='')}"
    )


def calendar_event_route(account: str, event_id: str) -> str:
    safe_account = _route_component(account, "Google account", 320)
    safe_event = _route_component(event_id, "Calendar event ID", 2_000)
    return (
        "https://calendar.google.com/calendar/u/"
        f"{urllib.parse.quote(safe_account, safe='@._+-')}/r/eventedit/"
        f"{urllib.parse.quote(safe_event, safe='')}"
    )


def local_google_evidence_route(
    connector_id: str,
    account_id: str,
    object_id: str,
) -> str:
    connector = connector_id.strip()
    if connector not in _CONNECTOR_IDS:
        raise ValueError("unsupported Google evidence connector")
    account = _route_component(account_id, "Google evidence account ID", 1_000)
    object_key = _route_component(object_id, "Google evidence object ID", 2_000)
    return (
        f"/evidence/google/{connector}/"
        f"{urllib.parse.quote(account, safe='')}/"
        f"{urllib.parse.quote(object_key, safe='')}"
    )


def _route_component(value: str, label: str, cap: int) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > cap or _CONTROL.search(normalized):
        raise ValueError(f"invalid {label}")
    if "://" in normalized or normalized in {".", ".."}:
        raise ValueError(f"invalid {label}")
    return normalized
