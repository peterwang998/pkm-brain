from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from .gmail_operations import GMAIL_DETECTOR_VERSION
from .google_routes import gmail_thread_route
from .operational_db import OperationalStoreUnavailableError, operational_connection
from .operational_shadow import (
    latest_handled_assessments,
    list_shadow_decisions,
    list_shadow_runs,
)
from .paths import BrainPaths
from .service import BrainService
from .util import now_iso


BRIEFING_SECTION_ORDER = (
    "focus",
    "urgent_overflow",
    "now_and_next",
    "upcoming",
    "overdue_and_due",
    "waiting",
    "attention",
    "awareness",
    "low_confidence",
    "changed",
    "suppressed",
)
ACTION_KINDS = {"commitment", "follow_up", "deadline", "attention"}
TERMINAL_STATES = {"resolved", "dismissed", "cancelled", "expired"}
HIGH_PRIORITY = 50
LOW_CONFIDENCE = 0.65
DEFAULT_FRESH_AFTER_SECONDS = 6 * 60 * 60


def build_operational_briefing(
    db_path: Path,
    *,
    timezone_name: str,
    policy_version: str,
    as_of: str | None = None,
    provider_accounts: Mapping[str, str] | None = None,
    required_sources: Sequence[str] = ("calendar", "gmail"),
    fresh_after_seconds: int = DEFAULT_FRESH_AFTER_SECONDS,
    run_context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    generated_at = _canonical_timestamp(as_of or now_iso())
    local_zone = ZoneInfo(timezone_name)
    now = _parse_timestamp(generated_at)
    local_now = now.astimezone(local_zone)
    today_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    tomorrow = today_start + timedelta(days=1)
    next_week = today_start + timedelta(days=7)

    latest_runs = [] if run_context is not None else list_shadow_runs(db_path, limit=1)
    latest_run = dict(run_context) if run_context is not None else (
        latest_runs[0] if latest_runs else None
    )
    coverage, status = _coverage_from_run(
        latest_run,
        required_sources=required_sources,
        now=now,
        fresh_after_seconds=fresh_after_seconds,
        policy_version=policy_version,
        gmail_detector_version=GMAIL_DETECTOR_VERSION,
    )
    handled = latest_handled_assessments(db_path)
    items = _load_items(db_path)

    cards = [
        _briefing_card(
            item,
            _current_assessment(item, handled, coverage),
            coverage=coverage,
            now=now,
            provider_account=(provider_accounts or {}).get(str(item["source_type"])),
        )
        for item in items
    ]
    active_cards = [card for card in cards if card["state"] not in TERMINAL_STATES]
    actionable = [
        card
        for card in active_cards
        if _is_action_candidate(card, now=now)
    ]
    actionable.sort(key=lambda card: _action_rank(card, now=now), reverse=True)
    focus = actionable[:5]
    focus_ids = {str(card["item_id"]) for card in focus}
    overflow = [
        card
        for card in actionable[5:]
        if int(card["priority"]) >= HIGH_PRIORITY
    ]

    event_cards = [card for card in active_cards if card["kind"] == "event"]
    now_and_next = sorted(
        [
            card
            for card in event_cards
            if _event_overlaps(card, today_start, tomorrow + timedelta(days=1))
        ],
        key=_start_sort_key,
    )[:20]
    upcoming = sorted(
        [
            card
            for card in event_cards
            if _starts_between(card, tomorrow, next_week)
        ],
        key=_start_sort_key,
    )[:50]
    overdue_and_due = sorted(
        [
            card
            for card in active_cards
            if card["kind"] in {"commitment", "follow_up", "deadline"}
            and card.get("due_at")
            and _parse_timestamp(str(card["due_at"])) < tomorrow.astimezone(timezone.utc)
        ],
        key=_due_sort_key,
    )[:100]
    waiting = sorted(
        [
            card
            for card in active_cards
            if card["kind"] == "waiting"
            or card["handled_verdict"] in {"responded_waiting", "being_handled"}
        ],
        key=lambda card: _action_rank(card, now=now),
        reverse=True,
    )[:100]
    attention = sorted(
        [
            card
            for card in active_cards
            if card["kind"] == "attention"
            or card["handled_verdict"] == "unknown"
            or str(card["item_id"]) in focus_ids
        ],
        key=lambda card: _action_rank(card, now=now),
        reverse=True,
    )[:100]
    low_confidence = sorted(
        [
            card
            for card in active_cards
            if float(card["confidence"]) < LOW_CONFIDENCE
            or card["reconciliation_status"] in {"ambiguous", "provisional"}
            or (
                card["kind"] != "event" and card["handled_verdict"] == "unknown"
            )
        ],
        key=lambda card: _action_rank(card, now=now),
        reverse=True,
    )[:100]
    changed = sorted(
        [card for card in active_cards if card["latest_event_type"] in {"updated", "rescheduled"}],
        key=lambda card: str(card.get("updated_at") or ""),
        reverse=True,
    )[:100]
    awareness = sorted(
        [
            card
            for card in active_cards
            if card["kind"] != "event"
            and card["handled_verdict"] in {
                "responded_waiting",
                "being_handled",
                "fulfilled",
            }
        ],
        key=lambda card: _awareness_sort_key(card, now=now),
        reverse=True,
    )[:100]

    suppressed = _suppressed_audit(db_path, latest_run)
    sections = {
        "focus": focus,
        "urgent_overflow": overflow,
        "now_and_next": now_and_next,
        "upcoming": upcoming,
        "overdue_and_due": overdue_and_due,
        "waiting": waiting,
        "attention": attention,
        "awareness": awareness,
        "low_confidence": low_confidence,
        "changed": changed,
        "suppressed": suppressed,
    }
    counts = {name: len(sections[name]) for name in BRIEFING_SECTION_ORDER}
    counts.update(
        {
            "active_items": len(active_cards),
            "terminal_items": len(cards) - len(active_cards),
            "action_candidates": len(actionable),
        }
    )
    all_clear = status == "complete" and not focus and not overflow
    if status != "complete":
        headline = "Shadow coverage is incomplete"
    elif focus:
        headline = f"{len(focus)} item{'s' if len(focus) != 1 else ''} need your attention"
    else:
        headline = "No current items need your action"
    return {
        "schema_version": 1,
        "generated_at": generated_at,
        "as_of": generated_at,
        "timezone": timezone_name,
        "policy_version": policy_version,
        "status": status,
        "headline": headline,
        "all_clear": all_clear,
        "shadow_mode": True,
        "run_id": latest_run.get("id") if latest_run else None,
        "coverage": coverage,
        "sections": sections,
        "counts": counts,
    }


def unavailable_operational_briefing(
    *,
    timezone_name: str,
    policy_version: str,
    reason: str,
    as_of: str | None = None,
) -> dict[str, Any]:
    timestamp = _canonical_timestamp(as_of or now_iso())
    return {
        "schema_version": 1,
        "generated_at": timestamp,
        "as_of": timestamp,
        "timezone": timezone_name,
        "policy_version": policy_version,
        "status": "unavailable",
        "headline": "Chief of Staff shadow trial is not initialized",
        "all_clear": False,
        "shadow_mode": True,
        "run_id": None,
        "coverage": {
            "calendar": {"status": "unavailable", "reason": reason},
            "gmail": {"status": "unavailable", "reason": reason},
        },
        "sections": {name: [] for name in BRIEFING_SECTION_ORDER},
        "counts": {name: 0 for name in BRIEFING_SECTION_ORDER},
    }


def operational_briefing_or_unavailable(
    paths: BrainPaths,
    *,
    timezone_name: str,
    policy_version: str,
    as_of: str | None = None,
    provider_accounts: Mapping[str, str] | None = None,
    required_sources: Sequence[str] = ("calendar", "gmail"),
    fresh_after_seconds: int = DEFAULT_FRESH_AFTER_SECONDS,
) -> dict[str, Any]:
    try:
        return build_operational_briefing(
            paths.ops_sqlite_path,
            timezone_name=timezone_name,
            policy_version=policy_version,
            as_of=as_of,
            provider_accounts=provider_accounts,
            required_sources=required_sources,
            fresh_after_seconds=fresh_after_seconds,
        )
    except OperationalStoreUnavailableError as exc:
        return unavailable_operational_briefing(
            timezone_name=timezone_name,
            policy_version=policy_version,
            reason=str(exc),
            as_of=as_of,
        )


def build_meeting_packet(
    paths: BrainPaths,
    item_id: str,
    *,
    generated_at: str | None = None,
    retrieval_budget: int = 3000,
) -> dict[str, Any]:
    timestamp = _canonical_timestamp(generated_at or now_iso())
    with operational_connection(paths.ops_sqlite_path, write=False) as conn:
        row = conn.execute(
            """
            SELECT i.*, o.evidence_refs
            FROM ops_items i
            JOIN ops_observations o ON o.id = i.current_observation_id
            WHERE i.id = ? AND i.item_kind = 'event'
            """,
            (item_id,),
        ).fetchone()
    if row is None:
        raise ValueError(f"unknown Calendar event item: {item_id}")
    item = dict(row)
    item["metadata"] = json.loads(str(item["metadata"]))
    evidence_refs = json.loads(str(item["evidence_refs"]))
    event_claims = [
        {
            "claim": f"Meeting: {item['title']}",
            "claim_type": "calendar_observation",
            "evidence_refs": evidence_refs,
        }
    ]
    if item.get("starts_at"):
        event_claims.append(
            {
                "claim": f"Starts at {item['starts_at']}",
                "claim_type": "calendar_observation",
                "evidence_refs": evidence_refs,
            }
        )
    if item.get("ends_at"):
        event_claims.append(
            {
                "claim": f"Ends at {item['ends_at']}",
                "claim_type": "calendar_observation",
                "evidence_refs": evidence_refs,
            }
        )
    location = item["metadata"].get("location")
    if location:
        event_claims.append(
            {
                "claim": f"Location: {location}",
                "claim_type": "calendar_observation",
                "evidence_refs": evidence_refs,
            }
        )

    retrieval: dict[str, Any]
    try:
        retrieval = BrainService(paths, read_only=True).retrieve_context(
            task=f"Prepare for the meeting '{item['title']}'. Find current decisions, commitments, people, projects, and relevant prior context.",
            project=str(item.get("project_ref") or "") or None,
            budget=max(500, min(int(retrieval_budget), 6000)),
            mode="compact",
            record_telemetry=False,
        )
    except Exception as exc:
        retrieval = {
            "retrieval_verdict": "unavailable",
            "retrieval_reasons": [str(exc)],
            "relevant_facts": [],
            "relevant_wiki_pages": [],
            "supporting_chunks": [],
        }

    knowledge_claims = []
    for fact in retrieval.get("relevant_facts") or []:
        statement = str(fact.get("statement") or "").strip()
        if not statement:
            continue
        knowledge_claims.append(
            {
                "claim": statement[:1000],
                "claim_type": "brain_fact",
                "evidence_refs": [
                    {"source_ref": str(source_id)}
                    for source_id in (fact.get("source_ids") or [])[:16]
                    if str(source_id).strip()
                ],
                "fact_id": fact.get("id"),
                "confidence": fact.get("truth_confidence", fact.get("confidence")),
            }
        )
    wiki_context = [
        {
            "title": str(page.get("title") or "")[:500],
            "path": page.get("relative_path"),
            "summary": str(page.get("summary") or "")[:1000],
            "source_ids": list(page.get("source_ids") or [])[:16],
        }
        for page in (retrieval.get("relevant_wiki_pages") or [])[:8]
    ]
    return {
        "schema_version": 1,
        "item_id": item_id,
        "generated_at": timestamp,
        "title": str(item["title"]),
        "event_claims": event_claims,
        "knowledge_claims": knowledge_claims,
        "wiki_context": wiki_context,
        "suggestions": [
            {
                "suggestion": "Confirm the desired outcome and decisions needed before the meeting.",
                "is_factual_claim": False,
            },
            {
                "suggestion": "Review open commitments and waiting items that involve this topic.",
                "is_factual_claim": False,
            },
        ],
        "coverage": {
            "calendar": "complete",
            "brain_retrieval": retrieval.get("retrieval_verdict", "unknown"),
        },
        "retrieval_reasons": retrieval.get("retrieval_reasons") or [],
    }


def local_evidence_route(
    *,
    source_type: str,
    account_key: str,
    source_ref: str,
    source_revision: str | None = None,
) -> str:
    query = {
        "source_type": source_type,
        "account_key": account_key,
        "source_ref": source_ref,
    }
    if source_revision:
        query["source_revision"] = source_revision
    return "/api/ops/evidence?" + urlencode(query)


def provider_native_route(
    card: Mapping[str, Any],
    *,
    provider_account: str | None = None,
) -> str | None:
    source_type = str(card.get("source_type") or "")
    account_key = str(card.get("account_key") or "")
    account = provider_account or (
        account_key.split(":", 1)[-1] if ":" in account_key else account_key
    )
    evidence_refs = card.get("evidence_refs") or []
    if source_type == "gmail":
        thread_id = _first_reference_value(evidence_refs, "thread_id")
        if thread_id and _safe_provider_id(thread_id) and "@" in account:
            return gmail_thread_route(account, thread_id)
    # Calendar event IDs are opaque API identifiers, not validated web routes.
    # Until the connector retains a provider-supplied, allowlisted htmlLink,
    # local revision-addressed evidence remains the only Calendar destination.
    return None


def _load_items(db_path: Path) -> list[dict[str, Any]]:
    with operational_connection(db_path, write=False) as conn:
        rows = conn.execute(
            """
            SELECT i.*, o.evidence_refs,
                   (
                     SELECT e.event_type FROM ops_item_events e
                     WHERE e.item_id = i.id
                     ORDER BY e.sequence DESC LIMIT 1
                   ) AS latest_event_type
            FROM ops_items i
            JOIN ops_observations o ON o.id = i.current_observation_id
            ORDER BY i.updated_at DESC, i.id
            """
        ).fetchall()
    output: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item["metadata"] = json.loads(str(item["metadata"]))
        item["evidence_refs"] = json.loads(str(item["evidence_refs"]))
        output.append(item)
    return output


def _briefing_card(
    item: Mapping[str, Any],
    assessment: Mapping[str, Any] | None,
    *,
    coverage: Mapping[str, Any],
    now: datetime,
    provider_account: str | None,
) -> dict[str, Any]:
    metadata = item.get("metadata") if isinstance(item.get("metadata"), Mapping) else {}
    evidence_refs = list(item.get("evidence_refs") or [])
    source_ref = _first_reference_value(evidence_refs, "source_ref") or (
        f"{item.get('source_type')}:{item.get('account_key')}:{item.get('stream_key')}:{item.get('source_key')}"
    )
    source_revision = _first_reference_value(evidence_refs, "source_revision")
    handled_verdict = str((assessment or {}).get("verdict") or "unknown")
    card: dict[str, Any] = {
        "id": str(item["id"]),
        "item_id": str(item["id"]),
        "kind": str(item["item_kind"]),
        "state": str(item["state"]),
        "title": str(item["title"]),
        "details": item.get("details"),
        "owner": str(item["owner"]),
        "counterparty": item.get("counterparty_entity_id"),
        "priority": int(item["priority"]),
        "confidence": float(item["confidence"]),
        "starts_at": item.get("starts_at"),
        "ends_at": item.get("ends_at"),
        "due_at": item.get("due_at"),
        "expires_at": item.get("expires_at"),
        "snoozed_until": item.get("snoozed_until"),
        "source_timezone": item.get("source_timezone"),
        "source_type": str(item["source_type"]),
        "account_key": str(item["account_key"]),
        "stream_key": str(item["stream_key"]),
        "source_key": str(item["source_key"]),
        "updated_at": item.get("updated_at"),
        "latest_event_type": str(item.get("latest_event_type") or "created"),
        "reconciliation_status": str(metadata.get("reconciliation_status") or "confirmed"),
        "handled_verdict": handled_verdict,
        "handled_confidence": float((assessment or {}).get("confidence") or 0.0),
        "handled_coverage": (assessment or {}).get("coverage") or coverage,
        "why_now": _why_now(item, handled_verdict=handled_verdict, now=now),
        "next_move": _next_move(item, handled_verdict=handled_verdict),
        "evidence_refs": evidence_refs,
        "local_evidence_route": local_evidence_route(
            source_type=str(item["source_type"]),
            account_key=str(item["account_key"]),
            source_ref=source_ref,
            source_revision=source_revision,
        ),
        "provider_route": None,
        "feedback_actions": _feedback_actions(item),
    }
    card["provider_route"] = provider_native_route(
        card,
        provider_account=provider_account,
    )
    return card


def _coverage_from_run(
    run: Mapping[str, Any] | None,
    *,
    required_sources: Sequence[str],
    now: datetime,
    fresh_after_seconds: int,
    policy_version: str,
    gmail_detector_version: str,
) -> tuple[dict[str, Any], str]:
    if fresh_after_seconds <= 0:
        raise ValueError("fresh_after_seconds must be positive")
    required = tuple(dict.fromkeys(str(source).strip() for source in required_sources))
    if not required or any(source not in {"calendar", "gmail"} for source in required):
        raise ValueError("required_sources must contain Calendar and/or Gmail")
    if run is None:
        return (
            {source: {"status": "unavailable", "reason": "never_run"} for source in required},
            "unavailable",
        )
    coverage = {
        str(source): dict(entry) if isinstance(entry, Mapping) else {}
        for source, entry in dict(run.get("coverage") or {}).items()
    }
    requested = {str(source) for source in run.get("requested_sources") or []}
    run_policy_version = str(run.get("policy_version") or "")
    run_detector_version = str(run.get("detector_version") or "")
    for source in required:
        if source not in requested:
            coverage[source] = {
                "status": "unavailable",
                "reason": "not_requested_in_latest_run",
            }
            continue
        entry = coverage.setdefault(
            source,
            {"status": "unavailable", "reason": "missing_coverage"},
        )
        if run_policy_version != policy_version:
            entry.update(
                {
                    "status": "partial",
                    "reason": "policy_version_mismatch",
                    "expected_policy_version": policy_version,
                    "run_policy_version": run_policy_version or None,
                }
            )
            continue
        if source == "gmail" and run_detector_version != gmail_detector_version:
            entry.update(
                {
                    "status": "partial",
                    "reason": "detector_version_mismatch",
                    "expected_detector_version": gmail_detector_version,
                    "run_detector_version": run_detector_version or None,
                }
            )
            continue
        fresh_at = entry.get("fresh_at")
        if str(entry.get("status") or "unavailable") == "complete":
            try:
                age = (now - _parse_timestamp(str(fresh_at))).total_seconds()
            except (TypeError, ValueError):
                age = fresh_after_seconds + 1
            if age > fresh_after_seconds:
                entry.update(
                    {
                        "status": "partial",
                        "reason": "stale",
                        "fresh_after_seconds": fresh_after_seconds,
                    }
                )
    statuses = {
        str(coverage[source].get("status") or "unavailable") for source in required
    }
    if run.get("status") == "complete" and statuses and statuses <= {"complete"}:
        return coverage, "complete"
    if run.get("status") == "failed" and not any(
        value == "complete" for value in statuses
    ):
        return coverage, "unavailable"
    return coverage, "partial"


def _suppressed_audit(
    db_path: Path,
    run: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    if run is None:
        return []
    # Incremental provider runs often contain no decision for an unchanged thread.
    # Keep the latest decision per source episode visible instead of making the
    # audit trail disappear whenever the newest incremental page is empty.
    decisions = list_shadow_decisions(db_path, limit=5000)
    latest_by_episode: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for decision in decisions:
        key = (
            str(decision["source_type"]),
            str(decision["account_key"]),
            str(decision["stream_key"]),
            str(decision["source_key"]),
        )
        latest_by_episode.setdefault(key, decision)
    output: list[dict[str, Any]] = []
    for decision in latest_by_episode.values():
        if decision["disposition"] == "surfaced":
            continue
        evidence_refs = list(decision.get("evidence_refs") or [])
        source_ref = _first_reference_value(evidence_refs, "source_ref") or (
            f"{decision['source_type']}:{decision['account_key']}:{decision['stream_key']}:{decision['source_key']}"
        )
        metadata = decision.get("metadata") or {}
        output.append(
            {
                "id": decision["id"],
                "source_type": decision["source_type"],
                "account_key": decision["account_key"],
                "source_key": decision["source_key"],
                "title": str(metadata.get("subject") or decision["source_key"])[:500],
                "disposition": decision["disposition"],
                "reason_code": decision["reason_code"],
                "confidence": decision["confidence"],
                "created_at": decision["created_at"],
                "evidence_refs": evidence_refs,
                "local_evidence_route": local_evidence_route(
                    source_type=str(decision["source_type"]),
                    account_key=str(decision["account_key"]),
                    source_ref=source_ref,
                    source_revision=_first_reference_value(
                        evidence_refs,
                        "source_revision",
                    ),
                ),
            }
        )
    return output[:200]


def _current_assessment(
    item: Mapping[str, Any],
    handled: Mapping[str, Mapping[str, Any]],
    coverage: Mapping[str, Any],
) -> Mapping[str, Any] | None:
    source = str(item.get("source_type") or "")
    source_coverage = coverage.get(source)
    if not isinstance(source_coverage, Mapping):
        return None
    if str(source_coverage.get("status") or "unavailable") != "complete":
        return None
    return handled.get(str(item["id"]))


def _is_action_candidate(card: Mapping[str, Any], *, now: datetime) -> bool:
    if card["kind"] not in ACTION_KINDS:
        return False
    snoozed_until = card.get("snoozed_until")
    if snoozed_until and _parse_timestamp(str(snoozed_until)) > now:
        return False
    verdict = str(card["handled_verdict"])
    if verdict == "needs_action":
        return True
    if verdict != "unknown":
        return False
    return str(card["owner"]) == "operator" or int(card["priority"]) >= HIGH_PRIORITY


def _action_rank(card: Mapping[str, Any], *, now: datetime) -> tuple[int, int, str]:
    score = int(card["priority"])
    due_at = card.get("due_at")
    if due_at:
        due = _parse_timestamp(str(due_at))
        if due < now:
            score += 40
        elif due <= now + timedelta(hours=24):
            score += 25
        elif due <= now + timedelta(days=7):
            score += 10
    if str(card["owner"]) == "operator":
        score += 15
    if str(card["handled_verdict"]) == "needs_action":
        score += 10
    if str(card["latest_event_type"]) in {"updated", "rescheduled"}:
        score += 5
    if float(card["confidence"]) < LOW_CONFIDENCE:
        score -= 5
    return score, int(float(card["confidence"]) * 1000), str(card["item_id"])


def _awareness_sort_key(
    card: Mapping[str, Any],
    *,
    now: datetime,
) -> tuple[int, str]:
    start = card.get("starts_at")
    if start:
        seconds = int((_parse_timestamp(str(start)) - now).total_seconds())
        return -abs(seconds), str(card["item_id"])
    return int(card["priority"]), str(card["item_id"])


def _start_sort_key(card: Mapping[str, Any]) -> tuple[str, str]:
    return str(card.get("starts_at") or "9999"), str(card["item_id"])


def _due_sort_key(card: Mapping[str, Any]) -> tuple[str, int, str]:
    return (
        str(card.get("due_at") or "9999"),
        -int(card["priority"]),
        str(card["item_id"]),
    )


def _event_overlaps(
    card: Mapping[str, Any],
    start: datetime,
    end: datetime,
) -> bool:
    starts_at = card.get("starts_at")
    if not starts_at:
        return False
    event_start = _parse_timestamp(str(starts_at))
    event_end = _parse_timestamp(str(card.get("ends_at") or starts_at))
    return event_start < end.astimezone(timezone.utc) and event_end >= start.astimezone(
        timezone.utc
    )


def _starts_between(
    card: Mapping[str, Any],
    start: datetime,
    end: datetime,
) -> bool:
    starts_at = card.get("starts_at")
    if not starts_at:
        return False
    event_start = _parse_timestamp(str(starts_at))
    return start.astimezone(timezone.utc) <= event_start < end.astimezone(timezone.utc)


def _why_now(
    item: Mapping[str, Any],
    *,
    handled_verdict: str,
    now: datetime,
) -> str:
    due_at = item.get("due_at")
    if due_at:
        due = _parse_timestamp(str(due_at))
        if due < now:
            return "Overdue and still active."
        if due <= now + timedelta(hours=24):
            return "Due within the next 24 hours."
    starts_at = item.get("starts_at")
    if starts_at and _parse_timestamp(str(starts_at)) <= now + timedelta(hours=24):
        return "Scheduled within the next 24 hours."
    if str(item.get("latest_event_type") or "") == "rescheduled":
        return "The source changed its timing."
    if handled_verdict == "needs_action":
        return "Current evidence indicates that you own the next move."
    if handled_verdict == "unknown":
        return "The next move could not be verified from current coverage."
    if handled_verdict == "responded_waiting":
        return "You responded and are waiting for the next result."
    if handled_verdict == "being_handled":
        return "Another identified party is progressing the next move."
    if handled_verdict == "fulfilled":
        return "Current evidence indicates that the requested result was supplied."
    return "It remains active in the current operational state."


def _next_move(item: Mapping[str, Any], *, handled_verdict: str) -> str:
    kind = str(item["item_kind"])
    if kind == "event":
        return "Prepare for or attend this event."
    if handled_verdict == "responded_waiting":
        return "Monitor for the response or result; follow up if it becomes stale."
    if handled_verdict == "being_handled":
        return "Monitor progress without duplicating the current owner’s work."
    if handled_verdict == "fulfilled":
        return "Verify the result before resolving the item."
    if handled_verdict == "unknown":
        return "Review the source evidence and confirm who owns the next move."
    if str(item["owner"]) == "operator":
        return str(item["title"])
    return "Review and assign the next move."


def _feedback_actions(item: Mapping[str, Any]) -> list[str]:
    state = str(item["state"])
    if state in TERMINAL_STATES:
        return ["restore"]
    return ["confirm", "done", "snooze", "dismiss", "incorrect"]


def _first_reference_value(refs: Any, key: str) -> str | None:
    if not isinstance(refs, list):
        return None
    for ref in refs:
        if isinstance(ref, Mapping) and str(ref.get(key) or "").strip():
            return str(ref[key])
    return None


def _safe_provider_id(value: str) -> bool:
    return bool(value) and len(value) <= 512 and all(
        char.isalnum() or char in {"-", "_"} for char in value
    )


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("operational timestamps must include a timezone")
    return parsed.astimezone(timezone.utc).replace(microsecond=0)


def _canonical_timestamp(value: str) -> str:
    return _parse_timestamp(value).isoformat()
