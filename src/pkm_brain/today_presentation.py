from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol


TODAY_SCHEMA_VERSION = 1
TODAY_STATUSES = {"available", "partial", "unavailable"}
FRESHNESS_STATES = {"fresh", "stale", "unknown"}
COVERAGE_STATES = {"complete", "partial", "unavailable"}
HANDLED_VERDICTS = {
    "needs_action",
    "responded_waiting",
    "being_handled",
    "fulfilled",
    "unknown",
}
FEEDBACK_ACTIONS = {
    "confirm",
    "correct",
    "done",
    "snooze",
    "dismiss",
    "dismiss_series",
    "restore",
}
ITEM_SECTIONS = {
    "focus",
    "urgent_overflow",
    "calendar_now",
    "calendar_next",
    "due_overdue",
    "waiting",
    "attention",
    "awareness",
    "uncertain",
    "ignored_suppressed",
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _string_tuple(values: Sequence[Any]) -> tuple[str, ...]:
    return tuple(text for value in values if (text := _optional_text(value)))


@dataclass(frozen=True)
class TodayEvidenceLink:
    source_type: str
    label: str
    reference: str
    brain_route: str | None = None
    provider_url: str | None = None

    def __post_init__(self) -> None:
        if not self.source_type.strip() or not self.label.strip() or not self.reference.strip():
            raise ValueError("today evidence requires source_type, label, and reference")
        if not self.brain_route and not self.provider_url:
            raise ValueError("today evidence requires a Brain route or provider URL")

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_type": self.source_type,
            "label": self.label,
            "reference": self.reference,
            "brain_route": self.brain_route,
            "provider_url": self.provider_url,
        }


@dataclass(frozen=True)
class TodayItem:
    id: str
    section: str
    title: str
    summary: str | None = None
    kind: str = "attention"
    state: str = "active"
    priority: str | None = None
    starts_at: str | None = None
    due_at: str | None = None
    ends_at: str | None = None
    owner: str | None = None
    counterparty: str | None = None
    confidence: float | None = None
    handled_verdict: str = "unknown"
    handled_reason: str | None = None
    reason_codes: tuple[str, ...] = ()
    evidence: tuple[TodayEvidenceLink, ...] = ()
    feedback_actions: tuple[str, ...] = ()
    meeting_brief_ready: bool = False

    def __post_init__(self) -> None:
        if not self.id.strip() or not self.title.strip():
            raise ValueError("today items require id and title")
        if self.section not in ITEM_SECTIONS:
            raise ValueError(f"unsupported today section: {self.section}")
        if self.handled_verdict not in HANDLED_VERDICTS:
            raise ValueError(f"unsupported handled verdict: {self.handled_verdict}")
        if self.confidence is not None and not 0 <= self.confidence <= 1:
            raise ValueError("today item confidence must be between 0 and 1")
        invalid_actions = set(self.feedback_actions).difference(FEEDBACK_ACTIONS)
        if invalid_actions:
            raise ValueError(
                f"unsupported today feedback actions: {', '.join(sorted(invalid_actions))}"
            )

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "section": self.section,
            "title": self.title,
            "summary": self.summary,
            "kind": self.kind,
            "state": self.state,
            "priority": self.priority,
            "starts_at": self.starts_at,
            "due_at": self.due_at,
            "ends_at": self.ends_at,
            "owner": self.owner,
            "counterparty": self.counterparty,
            "confidence": self.confidence,
            "handled_verdict": self.handled_verdict,
            "handled_reason": self.handled_reason,
            "reason_codes": list(self.reason_codes),
            "evidence": [item.as_dict() for item in self.evidence],
            "feedback_actions": list(self.feedback_actions),
            "meeting_brief_ready": self.meeting_brief_ready,
        }


@dataclass(frozen=True)
class TodayCoverage:
    source_id: str
    label: str
    state: str
    last_success_at: str | None = None
    detail: str | None = None
    deferred_count: int = 0

    def __post_init__(self) -> None:
        if not self.source_id.strip() or not self.label.strip():
            raise ValueError("today coverage requires source_id and label")
        if self.state not in COVERAGE_STATES:
            raise ValueError(f"unsupported coverage state: {self.state}")
        if self.deferred_count < 0:
            raise ValueError("coverage deferred_count cannot be negative")

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "label": self.label,
            "state": self.state,
            "last_success_at": self.last_success_at,
            "detail": self.detail,
            "deferred_count": self.deferred_count,
        }


@dataclass(frozen=True)
class TodayFreshness:
    state: str = "unknown"
    as_of: str | None = None
    age_seconds: int | None = None
    stale_after_seconds: int | None = None

    def __post_init__(self) -> None:
        if self.state not in FRESHNESS_STATES:
            raise ValueError(f"unsupported freshness state: {self.state}")
        if self.age_seconds is not None and self.age_seconds < 0:
            raise ValueError("freshness age_seconds cannot be negative")
        if self.stale_after_seconds is not None and self.stale_after_seconds <= 0:
            raise ValueError("freshness stale_after_seconds must be positive")

    def as_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "as_of": self.as_of,
            "age_seconds": self.age_seconds,
            "stale_after_seconds": self.stale_after_seconds,
        }


@dataclass(frozen=True)
class TodayFeedbackCapabilities:
    enabled: bool = False
    actions: tuple[str, ...] = ()
    unavailable_reason: str | None = None

    def __post_init__(self) -> None:
        invalid = set(self.actions).difference(FEEDBACK_ACTIONS | {"report_missing"})
        if invalid:
            raise ValueError(
                f"unsupported feedback capabilities: {', '.join(sorted(invalid))}"
            )

    def as_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "actions": list(self.actions),
            "unavailable_reason": self.unavailable_reason,
        }


@dataclass(frozen=True)
class TodayCalendarSuppression:
    id: str
    label: str
    hidden_count: int
    created_at: str
    next_starts_at: str | None = None

    def __post_init__(self) -> None:
        if not self.id.strip() or not self.label.strip():
            raise ValueError("calendar suppression requires id and label")
        if self.hidden_count < 0:
            raise ValueError("calendar suppression hidden_count cannot be negative")

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "hidden_count": self.hidden_count,
            "created_at": self.created_at,
            "next_starts_at": self.next_starts_at,
        }


@dataclass(frozen=True)
class TodayBriefing:
    status: str
    generated_at: str
    availability_reason: str | None = None
    briefing_id: str | None = None
    as_of: str | None = None
    timezone: str | None = None
    freshness: TodayFreshness = field(default_factory=TodayFreshness)
    coverage: tuple[TodayCoverage, ...] = ()
    focus: tuple[TodayItem, ...] = ()
    urgent_overflow_count: int = 0
    urgent_overflow: tuple[TodayItem, ...] = ()
    calendar_now: tuple[TodayItem, ...] = ()
    calendar_next: tuple[TodayItem, ...] = ()
    hidden_calendar_series: tuple[TodayCalendarSuppression, ...] = ()
    due_overdue: tuple[TodayItem, ...] = ()
    waiting: tuple[TodayItem, ...] = ()
    attention: tuple[TodayItem, ...] = ()
    awareness: tuple[TodayItem, ...] = ()
    uncertain: tuple[TodayItem, ...] = ()
    ignored_suppressed_count: int | None = None
    ignored_suppressed: tuple[TodayItem, ...] = ()
    feedback: TodayFeedbackCapabilities = field(
        default_factory=TodayFeedbackCapabilities
    )

    def __post_init__(self) -> None:
        if self.status not in TODAY_STATUSES:
            raise ValueError(f"unsupported today status: {self.status}")
        if len(self.focus) > 5:
            raise ValueError("today focus cannot contain more than five items")
        if self.urgent_overflow_count < len(self.urgent_overflow):
            raise ValueError("urgent overflow count cannot be smaller than its preview")
        if (
            self.ignored_suppressed_count is not None
            and self.ignored_suppressed_count < len(self.ignored_suppressed)
        ):
            raise ValueError(
                "ignored/suppressed count cannot be smaller than its preview"
            )

    @classmethod
    def unavailable(cls, reason: str) -> TodayBriefing:
        now = _utc_now()
        return cls(
            status="unavailable",
            generated_at=now,
            availability_reason=reason,
            freshness=TodayFreshness(state="unknown"),
            feedback=TodayFeedbackCapabilities(
                enabled=False,
                actions=(),
                unavailable_reason=reason,
            ),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": TODAY_SCHEMA_VERSION,
            "status": self.status,
            "availability_reason": self.availability_reason,
            "briefing_id": self.briefing_id,
            "generated_at": self.generated_at,
            "as_of": self.as_of,
            "timezone": self.timezone,
            "freshness": self.freshness.as_dict(),
            "coverage": [item.as_dict() for item in self.coverage],
            "focus": [item.as_dict() for item in self.focus],
            "urgent_overflow": {
                "count": self.urgent_overflow_count,
                "items": [item.as_dict() for item in self.urgent_overflow],
            },
            "calendar": {
                "now": [item.as_dict() for item in self.calendar_now],
                "next": [item.as_dict() for item in self.calendar_next],
                "hidden_series": [
                    item.as_dict() for item in self.hidden_calendar_series
                ],
            },
            "due_overdue": [item.as_dict() for item in self.due_overdue],
            "waiting": [item.as_dict() for item in self.waiting],
            "attention": [item.as_dict() for item in self.attention],
            "awareness": [item.as_dict() for item in self.awareness],
            "uncertain": [item.as_dict() for item in self.uncertain],
            "ignored_suppressed_count": max(
                self.ignored_suppressed_count or 0,
                len(self.ignored_suppressed),
            ),
            "ignored_suppressed": [
                item.as_dict() for item in self.ignored_suppressed
            ],
            "feedback": self.feedback.as_dict(),
        }


@dataclass(frozen=True)
class TodayFeedbackRequest:
    action: str
    note: str | None = None
    snoozed_until: str | None = None
    idempotency_key: str | None = None

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> TodayFeedbackRequest:
        action = str(payload.get("action") or "").strip()
        if action not in FEEDBACK_ACTIONS:
            raise ValueError(f"unsupported today feedback action: {action or '(missing)'}")
        note = _optional_text(payload.get("note"))
        if action == "correct" and not note:
            raise ValueError("note is required for correct feedback")
        snoozed_until = _optional_text(payload.get("snoozed_until"))
        if action == "snooze" and not snoozed_until:
            raise ValueError("snoozed_until is required for snooze feedback")
        return cls(
            action=action,
            note=note,
            snoozed_until=snoozed_until,
            idempotency_key=_optional_text(payload.get("idempotency_key")),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "note": self.note,
            "snoozed_until": self.snoozed_until,
            "idempotency_key": self.idempotency_key,
        }


@dataclass(frozen=True)
class TodayMissingReport:
    title: str
    detail: str | None = None
    source_hint: str | None = None
    idempotency_key: str | None = None

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> TodayMissingReport:
        title = _optional_text(payload.get("title"))
        if not title:
            raise ValueError("title is required when reporting a missing item")
        return cls(
            title=title,
            detail=_optional_text(payload.get("detail")),
            source_hint=_optional_text(payload.get("source_hint")),
            idempotency_key=_optional_text(payload.get("idempotency_key")),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "detail": self.detail,
            "source_hint": self.source_hint,
            "idempotency_key": self.idempotency_key,
        }


@dataclass(frozen=True)
class TodayFeedbackResult:
    status: str
    action: str
    message: str
    item_id: str | None = None
    recorded_at: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": TODAY_SCHEMA_VERSION,
            "status": self.status,
            "item_id": self.item_id,
            "action": self.action,
            "recorded_at": self.recorded_at,
            "message": self.message,
        }


class TodayPresentationService(Protocol):
    def briefing(self) -> TodayBriefing: ...

    def submit_feedback(
        self,
        item_id: str,
        request: TodayFeedbackRequest,
    ) -> TodayFeedbackResult: ...

    def report_missing(self, report: TodayMissingReport) -> TodayFeedbackResult: ...

    def restore_calendar_series(self, rule_id: str) -> TodayFeedbackResult: ...


class UnavailableTodayPresentationService:
    """Safe presentation stub used until an operational projection is attached."""

    def __init__(
        self,
        reason: str = "Operational briefing generation is not enabled yet.",
    ) -> None:
        self.reason = reason

    def briefing(self) -> TodayBriefing:
        return TodayBriefing.unavailable(self.reason)

    def submit_feedback(
        self,
        item_id: str,
        request: TodayFeedbackRequest,
    ) -> TodayFeedbackResult:
        return TodayFeedbackResult(
            status="unavailable",
            item_id=item_id,
            action=request.action,
            message=self.reason,
        )

    def report_missing(self, report: TodayMissingReport) -> TodayFeedbackResult:
        return TodayFeedbackResult(
            status="unavailable",
            action="report_missing",
            message=self.reason,
        )

    def restore_calendar_series(self, rule_id: str) -> TodayFeedbackResult:
        return TodayFeedbackResult(
            status="unavailable",
            item_id=rule_id,
            action="restore",
            message=self.reason,
        )


def today_briefing_payload(service: TodayPresentationService) -> dict[str, Any]:
    return service.briefing().as_dict()


def today_feedback_payload(
    service: TodayPresentationService,
    item_id: str,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    item_id = item_id.strip()
    if not item_id:
        raise ValueError("today item id is required")
    request = TodayFeedbackRequest.from_payload(payload)
    return service.submit_feedback(item_id, request).as_dict()


def today_missing_payload(
    service: TodayPresentationService,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    return service.report_missing(TodayMissingReport.from_payload(payload)).as_dict()


def today_calendar_series_restore_payload(
    service: TodayPresentationService,
    rule_id: str,
) -> dict[str, Any]:
    normalized = rule_id.strip()
    if not normalized:
        raise ValueError("calendar-series suppression id is required")
    return service.restore_calendar_series(normalized).as_dict()


def today_item(
    *,
    id: str,
    section: str,
    title: str,
    summary: str | None = None,
    reason_codes: Sequence[Any] = (),
    feedback_actions: Sequence[Any] = (),
    **values: Any,
) -> TodayItem:
    """Small construction helper for presentation adapters and fixture services."""

    return TodayItem(
        id=id,
        section=section,
        title=title,
        summary=summary,
        reason_codes=_string_tuple(reason_codes),
        feedback_actions=_string_tuple(feedback_actions),
        **values,
    )
