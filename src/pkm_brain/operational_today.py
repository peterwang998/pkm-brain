from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any

from .operational_briefing import operational_briefing_or_unavailable
from .operational_service import OperationalService
from .operational_shadow import list_shadow_runs
from .operations_policy import load_operations_policy
from .paths import BrainPaths
from .today_presentation import (
    TodayBriefing,
    TodayCoverage,
    TodayEvidenceLink,
    TodayFeedbackCapabilities,
    TodayFeedbackRequest,
    TodayFeedbackResult,
    TodayFreshness,
    TodayItem,
    TodayMissingReport,
)
from .util import now_iso


FRESH_AFTER_SECONDS = 6 * 60 * 60
TODAY_FEEDBACK_ACTIONS = (
    "confirm",
    "correct",
    "done",
    "snooze",
    "dismiss",
    "restore",
    "report_missing",
)


class OperationalTodayPresentationService:
    """Read the operational projection and route feedback through the daemon."""

    def __init__(self, paths: BrainPaths, operational_service: OperationalService) -> None:
        self.paths = paths
        self.operational_service = operational_service

    def briefing(self) -> TodayBriefing:
        try:
            policy = load_operations_policy(self.paths)
        except FileNotFoundError:
            return TodayBriefing.unavailable(
                "Connect the separate Calendar and Gmail read-only grants, then run Shadow."
            )
        except Exception as exc:
            return TodayBriefing.unavailable(f"Shadow policy is invalid: {exc}")
        projection = operational_briefing_or_unavailable(
            self.paths,
            timezone_name=policy.operator.timezone,
            policy_version=policy.version_ref,
            provider_accounts={
                "calendar": policy.operator.calendar.email,
                "gmail": policy.operator.gmail.email,
            },
            required_sources=tuple(
                source
                for source, enabled in (
                    ("calendar", policy.sources.calendar.enabled),
                    ("gmail", policy.sources.gmail.enabled),
                )
                if enabled
            ),
            fresh_after_seconds=FRESH_AFTER_SECONDS,
        )
        return today_briefing_from_operational(projection)

    def submit_feedback(
        self,
        item_id: str,
        request: TodayFeedbackRequest,
    ) -> TodayFeedbackResult:
        decision = {
            "confirm": "confirm",
            "correct": "incorrect",
            "done": "done",
            "snooze": "snooze",
            "dismiss": "dismiss",
            "restore": "restore",
        }[request.action]
        if request.action == "correct" and not (request.note or "").strip():
            raise ValueError("correction feedback requires a note")
        run_id = _latest_run_id(self.paths)
        result = self.operational_service.record_item_feedback(
            item_id,
            decision,
            note=request.note,
            snoozed_until=request.snoozed_until,
            idempotency_key=request.idempotency_key,
            run_id=run_id,
        )
        return TodayFeedbackResult(
            status="accepted",
            item_id=item_id,
            action=request.action,
            recorded_at=str(result["item"].get("updated_at") or now_iso()),
            message=_feedback_message(request.action),
        )

    def report_missing(self, report: TodayMissingReport) -> TodayFeedbackResult:
        source_hint = (report.source_hint or "").strip().casefold()
        source_type = source_hint if source_hint in {"calendar", "gmail"} else None
        summary = report.title.strip()
        if report.detail:
            summary = f"{summary}\n\n{report.detail.strip()}"
        result = self.operational_service.record_missing_report(
            summary=summary,
            run_id=_latest_run_id(self.paths),
            source_type=source_type,
            source_ref=report.source_hint if source_type is None else None,
            expected_kind=None,
            idempotency_key=report.idempotency_key,
        )
        return TodayFeedbackResult(
            status="accepted",
            action="report_missing",
            recorded_at=str(result["created_at"]),
            message="Missing item recorded for shadow evaluation.",
        )


def today_briefing_from_operational(
    projection: Mapping[str, Any],
    *,
    now: datetime | None = None,
) -> TodayBriefing:
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    status = {
        "complete": "available",
        "partial": "partial",
        "unavailable": "unavailable",
    }.get(str(projection.get("status") or "unavailable"), "unavailable")
    sections = projection.get("sections")
    if not isinstance(sections, Mapping):
        sections = {}
    counts = projection.get("counts")
    if not isinstance(counts, Mapping):
        counts = {}

    coverage = tuple(
        _today_coverage(source, value)
        for source, value in sorted(
            (projection.get("coverage") or {}).items(),
            key=lambda item: (item[0] != "calendar", item[0]),
        )
        if isinstance(value, Mapping)
    )
    as_of, freshness = _freshness(coverage, current=current)
    now_and_next = _unique_cards(
        list(_cards(sections, "now_and_next"))
        + list(_cards(sections, "upcoming"))
    )
    calendar_now: list[TodayItem] = []
    calendar_next: list[TodayItem] = []
    for card in now_and_next:
        starts = _timestamp(card.get("starts_at"))
        ends = _timestamp(card.get("ends_at")) or starts
        if starts and starts <= current and (ends is None or ends >= current):
            calendar_now.append(_today_item(card, "calendar_now"))
        elif starts and starts > current:
            calendar_next.append(_today_item(card, "calendar_next"))

    generated_at = str(projection.get("generated_at") or now_iso())
    availability_reason = None
    if status == "unavailable":
        availability_reason = str(
            projection.get("headline") or "Operational shadow data is unavailable."
        )
    elif status == "partial":
        availability_reason = "At least one source is incomplete; no all-clear is implied."
    feedback_enabled = status != "unavailable"
    return TodayBriefing(
        status=status,
        generated_at=generated_at,
        availability_reason=availability_reason,
        briefing_id=(
            str(projection.get("briefing_id") or projection.get("run_id"))
            if projection.get("briefing_id") or projection.get("run_id")
            else None
        ),
        as_of=as_of,
        timezone=str(projection.get("timezone") or "UTC"),
        freshness=freshness,
        coverage=coverage,
        focus=tuple(
            _today_item(card, "focus") for card in _cards(sections, "focus")[:5]
        ),
        urgent_overflow_count=_section_total(
            counts,
            "urgent_overflow",
            fallback=len(_cards(sections, "urgent_overflow")),
        ),
        urgent_overflow=tuple(
            _today_item(card, "urgent_overflow")
            for card in _cards(sections, "urgent_overflow")[:20]
        ),
        calendar_now=tuple(calendar_now[:10]),
        calendar_next=tuple(calendar_next[:10]),
        due_overdue=tuple(
            _today_item(card, "due_overdue")
            for card in _cards(sections, "overdue_and_due")
        ),
        waiting=tuple(
            _today_item(card, "waiting") for card in _cards(sections, "waiting")
        ),
        attention=tuple(
            _today_item(card, "attention")
            for card in _cards(sections, "attention")
        ),
        awareness=tuple(
            _today_item(card, "awareness")
            for card in _cards(sections, "awareness")
        ),
        uncertain=tuple(
            _today_item(card, "uncertain")
            for card in _cards(sections, "low_confidence")
        ),
        ignored_suppressed_count=_section_total(
            counts,
            "suppressed",
            fallback=len(_cards(sections, "suppressed")),
        ),
        ignored_suppressed=tuple(
            _today_suppressed(card) for card in _cards(sections, "suppressed")
        ),
        feedback=TodayFeedbackCapabilities(
            enabled=feedback_enabled,
            actions=TODAY_FEEDBACK_ACTIONS if feedback_enabled else (),
            unavailable_reason=(
                None if feedback_enabled else availability_reason
            ),
        ),
    )


def _today_item(card: Mapping[str, Any], section: str) -> TodayItem:
    evidence_refs = card.get("evidence_refs") or []
    source_ref = _first_reference(evidence_refs, "source_ref") or str(
        card.get("source_key") or card.get("item_id") or card.get("id")
    )
    source_type = str(card.get("source_type") or "unknown")
    evidence: tuple[TodayEvidenceLink, ...] = ()
    brain_route = _optional_string(card.get("local_evidence_route"))
    provider_url = _optional_string(card.get("provider_route"))
    if brain_route or provider_url:
        evidence = (
            TodayEvidenceLink(
                source_type=source_type,
                label="Local evidence",
                reference=source_ref,
                brain_route=brain_route,
                provider_url=provider_url,
            ),
        )
    actions = _today_feedback_actions(card.get("feedback_actions") or [])
    reason_codes = [
        str(card.get("reconciliation_status") or "confirmed"),
        str(card.get("latest_event_type") or "current"),
    ]
    return TodayItem(
        id=str(card.get("item_id") or card.get("id")),
        section=section,
        title=str(card.get("title") or "Untitled operational item")[:500],
        summary=_optional_string(card.get("next_move") or card.get("details")),
        kind=str(card.get("kind") or "attention"),
        state=str(card.get("state") or "active"),
        # An urgency score is useful for ranking verified work, but displaying it
        # as P0/P1 on an explicitly uncertain card overstates what the evidence
        # supports. Confidence and reconciliation state remain visible instead.
        priority=(
            None
            if section == "uncertain"
            else _priority_label(card.get("priority"))
        ),
        starts_at=_optional_string(card.get("starts_at")),
        due_at=_optional_string(card.get("due_at")),
        ends_at=_optional_string(card.get("ends_at")),
        owner=_optional_string(card.get("owner")),
        counterparty=_optional_string(card.get("counterparty")),
        confidence=_optional_confidence(card.get("confidence")),
        handled_verdict=str(card.get("handled_verdict") or "unknown"),
        handled_reason=_optional_string(card.get("why_now")),
        reason_codes=tuple(dict.fromkeys(reason_codes)),
        evidence=evidence,
        feedback_actions=actions,
    )


def _today_suppressed(card: Mapping[str, Any]) -> TodayItem:
    source_type = str(card.get("source_type") or "unknown")
    source_ref = _first_reference(card.get("evidence_refs") or [], "source_ref") or str(
        card.get("source_key") or card.get("id")
    )
    route = _optional_string(card.get("local_evidence_route"))
    evidence = (
        TodayEvidenceLink(
            source_type=source_type,
            label="Local evidence",
            reference=source_ref,
            brain_route=route,
        ),
    ) if route else ()
    reason = str(card.get("reason_code") or card.get("disposition") or "suppressed")
    return TodayItem(
        id=str(card.get("id")),
        section="ignored_suppressed",
        title=str(card.get("title") or card.get("source_key") or "Suppressed item")[:500],
        summary=f"{str(card.get('disposition') or 'suppressed').title()}: {reason.replace('_', ' ')}",
        kind="attention",
        state=str(card.get("disposition") or "suppressed"),
        confidence=_optional_confidence(card.get("confidence")),
        handled_verdict="unknown",
        handled_reason="Withheld from focus by the recorded shadow decision.",
        reason_codes=(reason,),
        evidence=evidence,
        feedback_actions=(),
    )


def _today_coverage(source: str, value: Mapping[str, Any]) -> TodayCoverage:
    state = str(value.get("status") or "unavailable")
    if state not in {"complete", "partial", "unavailable"}:
        state = "unavailable"
    detail = value.get("error")
    if not detail and value.get("reason"):
        detail = _coverage_reason_detail(str(value["reason"]), source=source)
    if not detail and source == "gmail" and value.get("thread_count") is not None:
        thread_count = max(0, int(value.get("thread_count") or 0))
        deferred = max(0, int(value.get("deferred_count") or 0))
        detail = f"{thread_count} threads checked"
        if deferred:
            detail += f" · {deferred} detector reviews deferred"
    if not detail and source == "calendar" and value.get("item_count") is not None:
        detail = f"{max(0, int(value.get('item_count') or 0))} events checked"
    if not detail and value.get("mode"):
        detail = f"{value['mode']} read-only sync"
    return TodayCoverage(
        source_id=str(source),
        label="Google Calendar" if source == "calendar" else "Gmail" if source == "gmail" else str(source).title(),
        state=state,
        last_success_at=_optional_string(value.get("fresh_at")),
        detail=_optional_string(detail),
        deferred_count=max(0, int(value.get("deferred_count") or 0)),
    )


def _coverage_reason_detail(reason: str, *, source: str) -> str:
    labels = {
        "missing_coverage": "This run is still gathering source coverage.",
        "never_run": "No shadow run has checked this source yet.",
        "not_requested_in_latest_run": "This source was not included in the latest run.",
        "policy_version_mismatch": "The latest result used an older operations policy.",
        "detector_version_mismatch": "Gmail needs a fresh detector review.",
        "stale": "The latest successful read is stale.",
        "detector_budget_exhausted": "The daily detector budget was reached; remaining reviews are deferred.",
    }
    if reason in labels:
        return labels[reason]
    source_label = "Calendar" if source == "calendar" else "Gmail" if source == "gmail" else source.title()
    readable = reason.replace("_", " ").strip().rstrip(".")
    return f"{source_label}: {readable}." if readable else f"{source_label} coverage is unavailable."


def _freshness(
    coverage: Sequence[TodayCoverage],
    *,
    current: datetime,
) -> tuple[str | None, TodayFreshness]:
    timestamps = [
        parsed
        for item in coverage
        if item.last_success_at and (parsed := _timestamp(item.last_success_at))
    ]
    if not timestamps:
        return None, TodayFreshness(
            state="unknown",
            stale_after_seconds=FRESH_AFTER_SECONDS,
        )
    as_of_value = min(timestamps)
    age = max(0, int((current - as_of_value).total_seconds()))
    return as_of_value.isoformat(), TodayFreshness(
        state="fresh" if age <= FRESH_AFTER_SECONDS else "stale",
        as_of=as_of_value.isoformat(),
        age_seconds=age,
        stale_after_seconds=FRESH_AFTER_SECONDS,
    )


def _cards(sections: Mapping[str, Any], key: str) -> list[Mapping[str, Any]]:
    values = sections.get(key)
    if not isinstance(values, list):
        return []
    return [value for value in values if isinstance(value, Mapping)]


def _section_total(counts: Mapping[str, Any], key: str, *, fallback: int) -> int:
    projection = counts.get("section_projection")
    if isinstance(projection, Mapping):
        section = projection.get(key)
        if isinstance(section, Mapping):
            total = section.get("total")
            if isinstance(total, int) and not isinstance(total, bool) and total >= 0:
                return max(fallback, total)
    total = counts.get(key)
    if isinstance(total, int) and not isinstance(total, bool) and total >= 0:
        return max(fallback, total)
    return fallback


def _unique_cards(cards: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    seen: set[str] = set()
    output: list[Mapping[str, Any]] = []
    for card in cards:
        key = str(card.get("item_id") or card.get("id"))
        if not key or key in seen:
            continue
        seen.add(key)
        output.append(card)
    return output


def _today_feedback_actions(values: Sequence[Any]) -> tuple[str, ...]:
    mapping = {
        "confirm": "confirm",
        "incorrect": "correct",
        "done": "done",
        "snooze": "snooze",
        "dismiss": "dismiss",
        "restore": "restore",
    }
    return tuple(
        dict.fromkeys(mapping[value] for raw in values if (value := str(raw)) in mapping)
    )


def _priority_label(value: Any) -> str | None:
    try:
        priority = int(value)
    except (TypeError, ValueError):
        return None
    if priority >= 90:
        return "P0"
    if priority >= 70:
        return "P1"
    if priority >= 40:
        return "P2"
    return "P3"


def _optional_confidence(value: Any) -> float | None:
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        return None
    return min(1.0, max(0.0, confidence))


def _timestamp(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _first_reference(values: Any, key: str) -> str | None:
    if not isinstance(values, list):
        return None
    for value in values:
        if isinstance(value, Mapping) and value.get(key):
            return str(value[key])
    return None


def _latest_run_id(paths: BrainPaths) -> str | None:
    try:
        runs = list_shadow_runs(paths.ops_sqlite_path, limit=1)
    except Exception:
        return None
    return str(runs[0]["id"]) if runs else None


def _feedback_message(action: str) -> str:
    return {
        "confirm": "Item confirmed as correct.",
        "correct": "Correction recorded and the item was dismissed as inaccurate.",
        "done": "Item marked done.",
        "snooze": "Item snoozed.",
        "dismiss": "Item dismissed.",
        "restore": "Item restored.",
    }[action]
