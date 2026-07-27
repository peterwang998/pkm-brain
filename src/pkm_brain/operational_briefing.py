from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from .gmail_operations import GMAIL_DETECTOR_VERSION
from .google_cache import GoogleCacheSecurityError, GoogleEvidenceCache
from .google_routes import gmail_thread_route
from .operational_db import OperationalStoreUnavailableError, operational_connection
from .operational_meeting_packets import (
    MEETING_PACKET_CONTENT_VERSION,
    meeting_packet_readiness,
)
from .operational_briefing_projection import (
    BRIEFING_SECTION_ORDER,
    bound_briefing_projection,
)
from .operational_shadow import (
    latest_handled_assessments,
    list_shadow_decisions,
    list_shadow_runs,
)
from .operational_suppressions import (
    active_calendar_series_keys,
    calendar_card_is_series_suppressed,
    list_calendar_series_suppressions,
)
from .operations_policy import load_operations_policy
from .paths import BrainPaths
from .service import BrainService
from .util import now_iso


ACTION_KINDS = {"commitment", "follow_up", "deadline", "attention"}
TERMINAL_STATES = {"resolved", "dismissed", "cancelled", "expired"}
HIGH_PRIORITY = 50
LOW_CONFIDENCE = 0.65
DEFAULT_FRESH_AFTER_SECONDS = 6 * 60 * 60
PRIMARY_ITEM_SECTION_ORDER = (
    "focus",
    "urgent_overflow",
    "now_and_next",
    "upcoming",
    "overdue_and_due",
    "waiting",
    "low_confidence",
    "attention",
    "awareness",
    "changed",
)


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
    prepared_meeting_ids = meeting_packet_readiness(db_path)
    hidden_calendar_keys = active_calendar_series_keys(db_path)
    hidden_calendar_series = list_calendar_series_suppressions(
        db_path,
        active_only=True,
        as_of=generated_at,
    )
    latest_gmail_decisions = _latest_gmail_decisions_by_thread(db_path)
    hidden_marketing_items = [
        item
        for item in items
        if _gmail_item_is_hidden_marketing(item, latest_gmail_decisions)
    ]
    visible_items = [
        item
        for item in items
        if not _gmail_item_is_hidden_marketing(item, latest_gmail_decisions)
    ]

    cards = [
        _briefing_card(
            item,
            _current_assessment(item, handled, coverage),
            coverage=coverage,
            now=now,
            provider_account=(provider_accounts or {}).get(str(item["source_type"])),
            recruiter_activity=_gmail_item_is_recruiter_activity(
                item,
                latest_gmail_decisions,
            ),
            meeting_brief_ready=str(item["id"]) in prepared_meeting_ids,
        )
        for item in visible_items
    ]
    active_cards_before_calendar_suppression = [
        card for card in cards if card["state"] not in TERMINAL_STATES
    ]
    hidden_calendar_occurrences = [
        card
        for card in active_cards_before_calendar_suppression
        if calendar_card_is_series_suppressed(card, hidden_calendar_keys)
    ]
    active_cards = [
        card
        for card in active_cards_before_calendar_suppression
        if not calendar_card_is_series_suppressed(card, hidden_calendar_keys)
    ]
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
            or (
                card["kind"] != "event"
                and card["handled_verdict"] == "unknown"
            )
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
                card["kind"] not in {"event", "attention"}
                and card["handled_verdict"] == "unknown"
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
    suppressed = _merge_hidden_marketing_audit(
        suppressed,
        hidden_marketing_items,
    )
    raw_sections = {
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
    facet_counts = {
        name: len(raw_sections[name]) for name in BRIEFING_SECTION_ORDER
    }
    raw_sections = _primary_item_sections(raw_sections)
    counts = {name: len(raw_sections[name]) for name in BRIEFING_SECTION_ORDER}
    counts.update(
        {
            "active_items": len(active_cards),
            "terminal_items": sum(
                card["state"] in TERMINAL_STATES for card in cards
            ),
            "hidden_calendar_occurrences": len(hidden_calendar_occurrences),
            "action_candidates": len(actionable),
            "section_facets": facet_counts,
        }
    )
    sections, counts = bound_briefing_projection(raw_sections, counts=counts)
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
        "hidden_calendar_series": hidden_calendar_series,
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
        "hidden_calendar_series": [],
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
    calendar_evidence = _meeting_calendar_evidence(
        paths,
        evidence_refs=evidence_refs,
    )
    operator_terms = _meeting_operator_terms(paths, calendar_evidence)
    meeting_terms = _meeting_match_terms(
        " ".join(
            (
                str(item.get("title") or ""),
                str((calendar_evidence or {}).get("details") or ""),
            )
        ),
        ignored_terms=operator_terms,
    )
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
    related_email_context = _meeting_related_email_context(
        paths,
        meeting_item=item,
        additional_context=(
            str(calendar_evidence.get("details") or "")
            if calendar_evidence is not None
            else ""
        ),
        ignored_terms=operator_terms,
    )
    for related in related_email_context:
        detail = str(related.get("details") or "").strip()
        claim = f"Related email: {related['title']}"
        if detail:
            claim += f" — {detail}"
        knowledge_claims.append(
            {
                "claim": claim[:1000],
                "claim_type": "gmail_operational_item",
                "evidence_refs": list(related.get("evidence_refs") or [])[:16],
                "fact_id": None,
                "confidence": related.get("confidence"),
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
    open_questions = [
        {
            "question": question,
            "source": "brain",
            "reference": str(row.get("id") or "").strip() or None,
            "fact_ids": [
                str(fact_id)
                for fact_id in (row.get("fact_ids") or [])[:16]
                if str(fact_id).strip()
            ],
        }
        for row in (retrieval.get("open_questions") or [])[:5]
        if (question := str(row.get("question") or "").strip())
    ]
    brief_knowledge_claims = [
        claim
        for claim in knowledge_claims
        if _meeting_candidate_is_relevant(
            meeting_terms,
            str(claim.get("claim") or ""),
            ignored_terms=operator_terms,
        )
    ]
    brief_wiki_context = [
        page
        for page in wiki_context
        if _meeting_candidate_is_relevant(
            meeting_terms,
            f"{page.get('title') or ''} {page.get('summary') or ''}",
            ignored_terms=operator_terms,
        )
    ]
    brief_open_questions = [
        question
        for question in open_questions
        if _meeting_candidate_is_relevant(
            meeting_terms,
            str(question.get("question") or ""),
            ignored_terms=operator_terms,
        )
    ]
    brief_context = _meeting_brief_context(
        item,
        calendar_evidence=calendar_evidence,
    )
    source_links = _meeting_source_links(
        item,
        evidence_refs=evidence_refs,
        calendar_evidence=calendar_evidence,
        wiki_context=brief_wiki_context,
        related_email_context=related_email_context,
    )
    return {
        "schema_version": 1,
        "content_version": MEETING_PACKET_CONTENT_VERSION,
        "item_id": item_id,
        "generated_at": timestamp,
        "title": str(item["title"]),
        "brief_context": brief_context,
        "event_claims": event_claims,
        "knowledge_claims": knowledge_claims,
        "brief_knowledge_claims": brief_knowledge_claims,
        "wiki_context": wiki_context,
        "brief_wiki_context": brief_wiki_context,
        "suggestions": [
            {
                "suggestion": "Confirm the desired outcome and decisions needed before the meeting.",
                "is_factual_claim": False,
            },
            {
                "suggestion": "Review open commitments and waiting items that involve this topic.",
                "is_factual_claim": False,
            },
            {
                "suggestion": "End with explicit owners and next steps for any decisions made.",
                "is_factual_claim": False,
            },
        ],
        "open_questions": open_questions,
        "brief_open_questions": brief_open_questions,
        "source_links": source_links,
        "coverage": {
            "calendar": "complete",
            "brain_retrieval": retrieval.get("retrieval_verdict", "unknown"),
        },
        "retrieval_reasons": retrieval.get("retrieval_reasons") or [],
    }


def _meeting_calendar_evidence(
    paths: BrainPaths,
    *,
    evidence_refs: Sequence[Mapping[str, Any]],
) -> Mapping[str, Any] | None:
    source_ref = _first_reference_value(evidence_refs, "source_ref")
    if not source_ref:
        return None
    cache_root = paths.home / "cache" / "google-evidence"
    if not cache_root.is_dir():
        return None
    try:
        payload = GoogleEvidenceCache.for_paths(paths).read_normalized(
            "calendar",
            source_ref,
            source_revision=_first_reference_value(
                evidence_refs,
                "source_revision",
            ),
        )
    except (GoogleCacheSecurityError, OSError, RuntimeError, TypeError, ValueError):
        return None
    return payload if isinstance(payload, Mapping) else None


def _meeting_brief_context(
    item: Mapping[str, Any],
    *,
    calendar_evidence: Mapping[str, Any] | None,
) -> dict[str, Any]:
    metadata = item.get("metadata")
    metadata = metadata if isinstance(metadata, Mapping) else {}
    calendar_notes = (
        str(calendar_evidence.get("details") or "").strip()[:4000]
        if calendar_evidence is not None
        else ""
    )
    return {
        "calendar_notes": calendar_notes or None,
        "calendar_notes_status": (
            "available"
            if calendar_notes
            else "not_provided"
            if calendar_evidence is not None
            else "source_unavailable"
        ),
        "starts_at": item.get("starts_at"),
        "ends_at": item.get("ends_at"),
        "location": (
            calendar_evidence.get("location")
            if calendar_evidence is not None
            else metadata.get("location")
        ),
        "organizer_email": (
            calendar_evidence.get("organizer_email")
            if calendar_evidence is not None
            else None
        ),
        "attendee_count": (
            calendar_evidence.get("attendee_count")
            if calendar_evidence is not None
            else metadata.get("attendee_count")
        ),
        "attendee_response": (
            calendar_evidence.get("attendee_response")
            if calendar_evidence is not None
            else metadata.get("attendee_response")
        ),
        "source_timezone": item.get("source_timezone"),
        "all_day": bool(metadata.get("all_day")),
    }


def _meeting_source_links(
    item: Mapping[str, Any],
    *,
    evidence_refs: Sequence[Mapping[str, Any]],
    calendar_evidence: Mapping[str, Any] | None,
    wiki_context: Sequence[Mapping[str, Any]],
    related_email_context: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    links: list[dict[str, Any]] = []
    source_ref = _first_reference_value(evidence_refs, "source_ref")
    if source_ref and calendar_evidence is not None:
        links.append(
            {
                "id": f"calendar:{source_ref}",
                "label": "Calendar event",
                "detail": "Open the retained local Calendar details",
                "source_type": "calendar",
                "reference": source_ref,
                "brain_route": local_evidence_route(
                    source_type="calendar",
                    account_key=str(item["account_key"]),
                    source_ref=source_ref,
                    source_revision=_first_reference_value(
                        evidence_refs,
                        "source_revision",
                    ),
                ),
                "provider_url": None,
                "wiki_path": None,
            }
        )
    for page in wiki_context:
        path = str(page.get("path") or "").strip()
        if not path:
            continue
        title = str(page.get("title") or "").strip() or "Brain page"
        links.append(
            {
                "id": f"wiki:{path}",
                "label": title[:500],
                "detail": "Open the relevant Brain page",
                "source_type": "wiki",
                "reference": path,
                "brain_route": None,
                "provider_url": None,
                "wiki_path": path,
            }
        )
    for related in related_email_context:
        source_ref = str(related.get("source_ref") or "").strip()
        account_key = str(related.get("account_key") or "").strip()
        if not source_ref or not account_key:
            continue
        links.append(
            {
                "id": f"gmail:{source_ref}",
                "label": f"Email: {str(related['title'])[:480]}",
                "detail": "Open the related source-backed email thread",
                "source_type": "gmail",
                "reference": source_ref,
                "brain_route": local_evidence_route(
                    source_type="gmail",
                    account_key=account_key,
                    source_ref=source_ref,
                    source_revision=related.get("source_revision"),
                ),
                "provider_url": related.get("provider_url"),
                "wiki_path": None,
            }
        )
    return links[:12]


_MEETING_MATCH_STOPWORDS = {
    "about",
    "after",
    "agenda",
    "and",
    "appointment",
    "appointments",
    "before",
    "calendar",
    "call",
    "catchup",
    "check",
    "checking",
    "checkin",
    "daily",
    "discussion",
    "event",
    "from",
    "general",
    "into",
    "meeting",
    "monthly",
    "next",
    "notes",
    "our",
    "project",
    "review",
    "scheduled",
    "session",
    "status",
    "sync",
    "team",
    "the",
    "this",
    "today",
    "tomorrow",
    "with",
    "update",
    "weekly",
}


def _meeting_related_email_context(
    paths: BrainPaths,
    *,
    meeting_item: Mapping[str, Any],
    additional_context: str = "",
    ignored_terms: set[str] | None = None,
) -> list[dict[str, Any]]:
    meeting_terms = _meeting_match_terms(
        f"{meeting_item.get('title') or ''} {additional_context}",
        ignored_terms=ignored_terms,
    )
    meeting_project = str(meeting_item.get("project_ref") or "").strip().casefold()
    meeting_counterparty = str(
        meeting_item.get("counterparty_entity_id") or ""
    ).strip().casefold()
    if not meeting_terms and not meeting_project and not meeting_counterparty:
        return []
    items = _load_items(paths.ops_sqlite_path)
    latest_decisions = _latest_gmail_decisions_by_thread(paths.ops_sqlite_path)
    try:
        provider_account = load_operations_policy(paths).operator.gmail.email
    except Exception:
        provider_account = None
    candidates: list[tuple[int, dict[str, Any]]] = []
    for item in items:
        if (
            str(item.get("source_type") or "") != "gmail"
            or str(item.get("state") or "active") in TERMINAL_STATES
            or _gmail_item_is_hidden_marketing(item, latest_decisions)
        ):
            continue
        candidate_terms = _meeting_match_terms(
            f"{item.get('title') or ''} {item.get('details') or ''}",
            ignored_terms=ignored_terms,
        )
        overlap = meeting_terms.intersection(candidate_terms)
        score = len(overlap) * 20
        if meeting_project and meeting_project == str(item.get("project_ref") or "").strip().casefold():
            score += 50
        if meeting_counterparty and meeting_counterparty == str(
            item.get("counterparty_entity_id") or ""
        ).strip().casefold():
            score += 40
        if score <= 0:
            continue
        refs = list(item.get("evidence_refs") or [])
        source_ref = _first_reference_value(refs, "source_ref")
        if not source_ref:
            continue
        card = {
            "source_type": "gmail",
            "account_key": str(item.get("account_key") or ""),
            "evidence_refs": refs,
        }
        candidates.append(
            (
                score,
                {
                    "item_id": str(item["id"]),
                    "title": str(item.get("title") or "Related email")[:500],
                    "details": str(item.get("details") or "")[:1000] or None,
                    "confidence": float(item.get("confidence") or 0.0),
                    "account_key": str(item.get("account_key") or ""),
                    "source_ref": source_ref,
                    "source_revision": _first_reference_value(
                        refs,
                        "source_revision",
                    ),
                    "evidence_refs": refs,
                    "provider_url": provider_native_route(
                        card,
                        provider_account=provider_account,
                    ),
                },
            )
        )
    candidates.sort(
        key=lambda value: (
            value[0],
            float(value[1]["confidence"]),
            str(value[1]["item_id"]),
        ),
        reverse=True,
    )
    return [value for _score, value in candidates[:5]]


def _meeting_match_terms(
    value: str,
    *,
    ignored_terms: set[str] | None = None,
) -> set[str]:
    ignored = ignored_terms or set()
    return {
        token
        for token in re.findall(r"[a-z0-9]+", value.casefold())
        if len(token) >= 3
        and token not in _MEETING_MATCH_STOPWORDS
        and token not in ignored
        and not token.isdigit()
    }


def _meeting_candidate_is_relevant(
    meeting_terms: set[str],
    candidate: str,
    *,
    ignored_terms: set[str] | None = None,
) -> bool:
    if not meeting_terms:
        return False
    candidate_terms = _meeting_match_terms(
        candidate,
        ignored_terms=ignored_terms,
    )
    return bool(meeting_terms.intersection(candidate_terms))


def _meeting_operator_terms(
    paths: BrainPaths,
    calendar_evidence: Mapping[str, Any] | None,
) -> set[str]:
    emails: list[str] = []
    try:
        operator = load_operations_policy(paths).operator
        emails.extend((operator.calendar.email, operator.gmail.email))
    except (FileNotFoundError, OSError, ValueError):
        pass
    if calendar_evidence and calendar_evidence.get("organizer_self") is True:
        emails.append(str(calendar_evidence.get("organizer_email") or ""))
    terms: set[str] = set()
    for email in emails:
        local_part = str(email).partition("@")[0]
        terms.update(
            token
            for token in re.findall(r"[a-z0-9]+", local_part.casefold())
            if len(token) >= 3 and not token.isdigit()
        )
    return terms


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
    recruiter_activity: bool = False,
    meeting_brief_ready: bool = False,
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
        "recurring_event_id": metadata.get("recurring_event_id"),
        "meeting_brief_ready": meeting_brief_ready,
        "recruiter_activity": recruiter_activity,
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
            # Diagnostics on the retained entry describe the superseded run.
            # Do not let them masquerade as a current provider failure after
            # policy compatibility has become the authoritative limitation.
            entry.pop("error", None)
            entry.pop("sync_error", None)
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
            entry.pop("error", None)
            entry.pop("sync_error", None)
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


def _latest_gmail_decisions_by_thread(
    db_path: Path,
) -> dict[tuple[str, str], Mapping[str, Any]]:
    output: dict[tuple[str, str], Mapping[str, Any]] = {}
    for decision in list_shadow_decisions(db_path, limit=5000):
        if str(decision.get("source_type") or "") != "gmail":
            continue
        key = (
            str(decision.get("account_key") or ""),
            str(decision.get("source_key") or ""),
        )
        output.setdefault(key, decision)
    return output


def _gmail_item_is_hidden_marketing(
    item: Mapping[str, Any],
    latest_decisions: Mapping[tuple[str, str], Mapping[str, Any]],
) -> bool:
    if (
        str(item.get("source_type") or "") != "gmail"
        or str(item.get("state") or "active") in TERMINAL_STATES
    ):
        return False
    evidence_refs = item.get("evidence_refs") or []
    thread_id = _first_reference_value(evidence_refs, "thread_id") or str(
        item.get("source_key") or ""
    ).partition(":")[0]
    decision = latest_decisions.get(
        (str(item.get("account_key") or ""), thread_id)
    )
    decision_reason = str((decision or {}).get("reason_code") or "").casefold()
    decision_disposition = str((decision or {}).get("disposition") or "")
    if decision_disposition == "surfaced" and decision_reason == "recruiter_activity":
        return False
    return decision_disposition != "surfaced" and "marketing" in decision_reason


def _gmail_item_is_recruiter_activity(
    item: Mapping[str, Any],
    latest_decisions: Mapping[tuple[str, str], Mapping[str, Any]],
) -> bool:
    if str(item.get("source_type") or "") != "gmail":
        return False
    evidence_refs = item.get("evidence_refs") or []
    thread_id = _first_reference_value(evidence_refs, "thread_id") or str(
        item.get("source_key") or ""
    ).partition(":")[0]
    decision = latest_decisions.get(
        (str(item.get("account_key") or ""), thread_id)
    )
    return bool(
        decision
        and str(decision.get("disposition") or "") == "surfaced"
        and str(decision.get("reason_code") or "") == "recruiter_activity"
    )


def _merge_hidden_marketing_audit(
    suppressed: Sequence[Mapping[str, Any]],
    hidden_items: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    output = [dict(value) for value in suppressed]
    existing = {
        (
            str(value.get("account_key") or ""),
            _first_reference_value(value.get("evidence_refs") or [], "thread_id")
            or str(value.get("source_key") or "").partition(":")[0],
        )
        for value in output
        if str(value.get("source_type") or "") == "gmail"
    }
    for item in hidden_items:
        evidence_refs = list(item.get("evidence_refs") or [])
        thread_id = _first_reference_value(evidence_refs, "thread_id") or str(
            item.get("source_key") or ""
        ).partition(":")[0]
        key = (str(item.get("account_key") or ""), thread_id)
        if key in existing:
            continue
        existing.add(key)
        source_ref = _first_reference_value(evidence_refs, "source_ref") or (
            f"gmail:{item.get('account_key')}:{thread_id}"
        )
        output.append(
            {
                "id": f"marketing-hidden:{item['id']}",
                "source_type": "gmail",
                "account_key": str(item.get("account_key") or ""),
                "source_key": thread_id,
                "title": str(item.get("title") or "Marketing update")[:500],
                "disposition": "suppressed",
                "reason_code": "marketing_update",
                "confidence": max(float(item.get("confidence") or 0.0), 0.99),
                "created_at": item.get("updated_at") or item.get("created_at"),
                "evidence_refs": evidence_refs,
                "local_evidence_route": local_evidence_route(
                    source_type="gmail",
                    account_key=str(item.get("account_key") or ""),
                    source_ref=source_ref,
                    source_revision=_first_reference_value(
                        evidence_refs,
                        "source_revision",
                    ),
                ),
            }
        )
    return output[:200]


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
    if (
        float(card["confidence"]) < LOW_CONFIDENCE
        or str(card["reconciliation_status"]) != "confirmed"
    ):
        return False
    if (
        bool(card.get("recruiter_activity"))
        and str(card.get("kind") or "") == "attention"
        and not card.get("due_at")
        and not card.get("starts_at")
    ):
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


def _primary_item_sections(
    sections: Mapping[str, Sequence[Mapping[str, Any]]],
) -> dict[str, list[dict[str, Any]]]:
    """Assign every operational item to one user-visible briefing section.

    Section membership is still available in section_facets counts, but repeating
    the same card across Focus, Attention, and Uncertain makes the briefing look
    noisier than the underlying ledger. Suppressed decisions are audit records, not
    operational items, and therefore retain their independent preview.
    """

    output = {name: [] for name in BRIEFING_SECTION_ORDER}
    seen: set[str] = set()
    for section in PRIMARY_ITEM_SECTION_ORDER:
        for value in sections.get(section, ()):
            card = dict(value)
            item_id = str(card.get("item_id") or card.get("id") or "")
            if not item_id or item_id in seen:
                continue
            output[section].append(card)
            seen.add(item_id)
    output["suppressed"] = [dict(value) for value in sections.get("suppressed", ())]
    return output


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
    actions = ["confirm", "done", "snooze", "dismiss", "incorrect"]
    metadata = item.get("metadata")
    if (
        str(item.get("source_type") or "") == "calendar"
        and isinstance(metadata, Mapping)
        and str(metadata.get("recurring_event_id") or "").strip()
    ):
        actions.append("dismiss_series")
    return actions


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
