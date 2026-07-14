from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping, Protocol, Sequence

from .google_normalization import NormalizedGmailMessage, NormalizedGmailThread
from .llm import LLMProvider, complete_json


GMAIL_DETECTOR_VERSION = "gmail-operations-v1"
ITEM_KINDS = {"commitment", "waiting", "follow_up", "deadline", "attention"}
ITEM_OWNERS = {"operator", "other", "shared", "unknown"}
OPERATIONS = {"create", "update", "resolve", "cancel", "none", "needs_reconciliation"}
HANDLED_VERDICTS = {
    "needs_action",
    "responded_waiting",
    "being_handled",
    "fulfilled",
    "unknown",
}
SUPPRESSION_REASONS = {
    "marketing_no_action",
    "bulk_no_action",
    "transactional_no_current_action",
    "human_no_current_action",
    "empty_thread",
    "model_ignore",
}
_ACTION_SIGNAL = re.compile(
    r"\b(?:can you|could you|would you|please|need you|let me know|follow[ -]?up|"
    r"send|review|approve|confirm|sign|pay|complete|submit|reply|respond|schedule|"
    r"reschedule|cancel|renew|decide|decision|action required|required action|"
    r"remind|by (?:today|tomorrow|monday|tuesday|wednesday|thursday|friday))\b",
    re.IGNORECASE,
)
_COMMITMENT_SIGNAL = re.compile(
    r"\b(?:i(?:'ll| will| can| am going to)|let me|we(?:'ll| will| can))\b",
    re.IGNORECASE,
)
_HIGH_CONSEQUENCE_SIGNAL = re.compile(
    r"\b(?:urgent|past due|overdue|expires?|renewal|deadline|security|fraud|"
    r"breach|legal|lawsuit|tax|payment|invoice|bill|flight|reservation|hotel|"
    r"delivery|appointment|interview|cancelled|canceled|delayed|changed)\b",
    re.IGNORECASE,
)
_MARKETING_SIGNAL = re.compile(
    r"\b(?:unsubscribe|shop now|sale|discount|offer|newsletter|digest|promotion)\b",
    re.IGNORECASE,
)
_PRIORITY_WORDS = {
    "critical": 90,
    "high": 70,
    "normal": 40,
    "low": 15,
    "awareness": 0,
}


class GmailDetectorError(RuntimeError):
    pass


class GmailDetectorPolicy(Protocol):
    detector_calls_per_day: int
    detector_input_tokens_per_day: int
    detector_total_tokens_per_day: int


@dataclass(frozen=True)
class GmailDetectorBudget:
    max_calls: int = 100
    max_input_tokens: int = 150_000
    max_total_tokens: int = 180_000
    max_batch_threads: int = 12
    max_batch_chars: int = 48_000

    def validated(self) -> GmailDetectorBudget:
        values = (
            self.max_calls,
            self.max_input_tokens,
            self.max_total_tokens,
            self.max_batch_threads,
            self.max_batch_chars,
        )
        if any(isinstance(value, bool) or int(value) <= 0 for value in values):
            raise ValueError("Gmail detector budgets must be positive integers")
        if self.max_total_tokens < self.max_input_tokens:
            raise ValueError("total token budget cannot be smaller than input budget")
        if self.max_batch_threads > 50 or self.max_batch_chars > 200_000:
            raise ValueError("Gmail detector batch bounds are unsafe")
        return self


@dataclass(frozen=True)
class GmailPreclassification:
    thread_id: str
    should_detect: bool
    reason_code: str
    high_consequence: bool
    direct_operator_thread: bool
    estimated_input_tokens: int


@dataclass(frozen=True)
class GmailOperationalCandidate:
    detector_key: str
    operation: str
    kind: str
    title: str
    owner: str
    confidence: float
    priority: int
    evidence_message_ids: tuple[str, ...]
    handled_verdict: str
    handled_confidence: float
    due_at: str | None = None
    starts_at: str | None = None
    ends_at: str | None = None
    expires_at: str | None = None
    counterparty: str | None = None
    reason: str | None = None
    reconciliation_status: str = "confirmed"


@dataclass(frozen=True)
class GmailThreadDetection:
    thread_id: str
    disposition: str
    reason_code: str
    candidates: tuple[GmailOperationalCandidate, ...] = ()
    confidence: float = 0.0
    error: str | None = None


@dataclass(frozen=True)
class GmailDetectionBatchResult:
    detections: tuple[GmailThreadDetection, ...]
    requests: int
    estimated_input_tokens: int
    estimated_output_tokens: int
    deferred_count: int
    model_thread_count: int
    deterministic_suppressed_count: int
    coverage_complete: bool
    errors: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, Any]:
        return {
            "detections": [
                {
                    "thread_id": detection.thread_id,
                    "disposition": detection.disposition,
                    "reason_code": detection.reason_code,
                    "confidence": detection.confidence,
                    "error": detection.error,
                    "candidates": [candidate.__dict__ for candidate in detection.candidates],
                }
                for detection in self.detections
            ],
            "usage": {
                "requests": self.requests,
                "estimated_input_tokens": self.estimated_input_tokens,
                "estimated_output_tokens": self.estimated_output_tokens,
                "estimated_total_tokens": (
                    self.estimated_input_tokens + self.estimated_output_tokens
                ),
            },
            "deferred_count": self.deferred_count,
            "model_thread_count": self.model_thread_count,
            "deterministic_suppressed_count": self.deterministic_suppressed_count,
            "coverage_complete": self.coverage_complete,
            "errors": list(self.errors),
        }


def budget_from_operations_policy(policy: Any) -> GmailDetectorBudget:
    gmail = policy.budgets.gmail
    return GmailDetectorBudget(
        max_calls=int(gmail.detector_calls_per_day),
        max_input_tokens=int(gmail.detector_input_tokens_per_day),
        max_total_tokens=int(gmail.detector_total_tokens_per_day),
    ).validated()


def preclassify_gmail_thread(
    thread: NormalizedGmailThread,
    *,
    operator_emails: Sequence[str],
) -> GmailPreclassification:
    operator = {email.strip().casefold() for email in operator_emails if email.strip()}
    content = _thread_signal_text(thread)
    estimated = _estimate_tokens(_thread_model_payload(thread, active_items=()))
    if not thread.messages or not content.strip():
        return GmailPreclassification(
            thread_id=thread.thread_id,
            should_detect=False,
            reason_code="empty_thread",
            high_consequence=False,
            direct_operator_thread=False,
            estimated_input_tokens=estimated,
        )
    high_consequence = bool(_HIGH_CONSEQUENCE_SIGNAL.search(content))
    action_signal = bool(_ACTION_SIGNAL.search(content))
    commitment_signal = any(
        message.outgoing and _COMMITMENT_SIGNAL.search(message.body or "")
        for message in thread.messages
    )
    direct_operator_thread = any(
        not message.outgoing
        and bool(operator.intersection(message.to_addresses + message.cc_addresses))
        for message in thread.messages
    )
    has_question = any(
        not message.outgoing and "?" in message.body
        for message in thread.messages
    )
    should_detect = (
        thread.message_class == "human"
        or high_consequence
        or action_signal
        or commitment_signal
        or (direct_operator_thread and has_question)
    )
    if should_detect:
        reason = (
            "high_consequence_signal"
            if high_consequence
            else "human_correspondence"
            if thread.message_class == "human"
            else "operational_language"
        )
    elif thread.message_class in {"bulk", "marketing"} or _MARKETING_SIGNAL.search(content):
        reason = "marketing_no_action"
    elif thread.message_class == "transactional":
        reason = "transactional_no_current_action"
    else:
        reason = "human_no_current_action"
    return GmailPreclassification(
        thread_id=thread.thread_id,
        should_detect=should_detect,
        reason_code=reason,
        high_consequence=high_consequence,
        direct_operator_thread=direct_operator_thread,
        estimated_input_tokens=estimated,
    )


def detect_gmail_threads(
    threads: Sequence[NormalizedGmailThread],
    *,
    operator_emails: Sequence[str],
    timezone_name: str,
    policy_version: str,
    budget: GmailDetectorBudget,
    llm_provider: LLMProvider,
    active_items_by_thread: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
) -> GmailDetectionBatchResult:
    budget.validated()
    if len(threads) != len({thread.thread_id for thread in threads}):
        raise ValueError("a Gmail detector run cannot contain duplicate thread IDs")
    active_items_by_thread = active_items_by_thread or {}
    preclassified = [
        preclassify_gmail_thread(thread, operator_emails=operator_emails)
        for thread in threads
    ]
    by_id = {thread.thread_id: thread for thread in threads}
    preclassification_by_id = {decision.thread_id: decision for decision in preclassified}
    detections: dict[str, GmailThreadDetection] = {}
    model_entries: list[tuple[NormalizedGmailThread, GmailPreclassification]] = []
    for decision in preclassified:
        if decision.should_detect:
            model_entries.append((by_id[decision.thread_id], decision))
            continue
        detections[decision.thread_id] = GmailThreadDetection(
            thread_id=decision.thread_id,
            disposition="suppressed",
            reason_code=decision.reason_code,
            confidence=0.99,
        )

    model_entries.sort(
        key=lambda pair: (
            pair[1].high_consequence,
            pair[1].direct_operator_thread,
            pair[0].updated_at or "",
        ),
        reverse=True,
    )
    requests = 0
    estimated_input_tokens = 0
    estimated_output_tokens = 0
    deferred_count = 0
    errors: list[str] = []
    index = 0
    while index < len(model_entries):
        batch: list[NormalizedGmailThread] = []
        batch_chars = 0
        batch_input_tokens = 0
        while index < len(model_entries) and len(batch) < budget.max_batch_threads:
            thread, _preclassification = model_entries[index]
            payload = _thread_model_payload(
                thread,
                active_items=active_items_by_thread.get(thread.thread_id, ()),
            )
            encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            estimated = _estimate_tokens(payload)
            if len(encoded) > budget.max_batch_chars:
                detections[thread.thread_id] = GmailThreadDetection(
                    thread_id=thread.thread_id,
                    disposition="deferred",
                    reason_code="detector_thread_oversized",
                )
                deferred_count += 1
                index += 1
                continue
            if batch and batch_chars + len(encoded) > budget.max_batch_chars:
                break
            if (
                requests >= budget.max_calls
                or estimated_input_tokens + batch_input_tokens + estimated
                > budget.max_input_tokens
                or estimated_input_tokens
                + estimated_output_tokens
                + batch_input_tokens
                + estimated
                + 800
                > budget.max_total_tokens
            ):
                for remaining, _decision in model_entries[index:]:
                    detections[remaining.thread_id] = GmailThreadDetection(
                        thread_id=remaining.thread_id,
                        disposition="deferred",
                        reason_code="detector_budget_exhausted",
                    )
                    deferred_count += 1
                index = len(model_entries)
                batch = []
                break
            batch.append(thread)
            batch_chars += len(encoded)
            batch_input_tokens += estimated
            index += 1
        if not batch:
            continue
        prompt = _detector_prompt(
            batch,
            operator_emails=operator_emails,
            timezone_name=timezone_name,
            policy_version=policy_version,
            active_items_by_thread=active_items_by_thread,
        )
        requests += 1
        estimated_input_tokens += batch_input_tokens
        try:
            response = complete_json(
                prompt,
                schema={"type": "object", "required": ["threads"]},
                llm_provider=llm_provider,
                max_attempts=2,
            )
            parsed = _parse_batch_response(response, batch)
            for result in parsed:
                decision = preclassification_by_id[result.thread_id]
                if result.disposition == "suppressed" and decision.high_consequence:
                    detections[result.thread_id] = _high_consequence_fallback(
                        by_id[result.thread_id],
                        reason_code="high_consequence_model_uncertain",
                    )
                else:
                    detections[result.thread_id] = result
            estimated_output_tokens += _estimate_tokens(response)
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"[:1000]
            errors.append(message)
            for thread in batch:
                if preclassification_by_id[thread.thread_id].high_consequence:
                    fallback = _high_consequence_fallback(
                        thread,
                        reason_code="high_consequence_detector_error",
                    )
                    detections[thread.thread_id] = GmailThreadDetection(
                        **{**fallback.__dict__, "error": message}
                    )
                else:
                    detections[thread.thread_id] = GmailThreadDetection(
                        thread_id=thread.thread_id,
                        disposition="error",
                        reason_code="detector_error",
                        error=message,
                    )

    ordered = tuple(
        detections.get(
            thread.thread_id,
            GmailThreadDetection(
                thread_id=thread.thread_id,
                disposition="error",
                reason_code="missing_detector_result",
                error="detector produced no result for the thread",
            ),
        )
        for thread in threads
    )
    coverage_complete = not deferred_count and not errors and all(
        detection.disposition != "error" for detection in ordered
    )
    return GmailDetectionBatchResult(
        detections=ordered,
        requests=requests,
        estimated_input_tokens=estimated_input_tokens,
        estimated_output_tokens=estimated_output_tokens,
        deferred_count=deferred_count,
        model_thread_count=len(model_entries) - deferred_count,
        deterministic_suppressed_count=len(threads) - len(model_entries),
        coverage_complete=coverage_complete,
        errors=tuple(errors),
    )


def _parse_batch_response(
    response: Mapping[str, Any],
    batch: Sequence[NormalizedGmailThread],
) -> tuple[GmailThreadDetection, ...]:
    raw_results = response.get("threads")
    if not isinstance(raw_results, list):
        raise GmailDetectorError("detector response threads must be a list")
    allowed = {thread.thread_id: thread for thread in batch}
    seen: set[str] = set()
    output: list[GmailThreadDetection] = []
    for raw in raw_results:
        if not isinstance(raw, Mapping):
            raise GmailDetectorError("detector thread result must be an object")
        thread_id = str(raw.get("thread_id") or "").strip()
        if thread_id not in allowed or thread_id in seen:
            raise GmailDetectorError("detector returned an unknown or duplicate thread")
        seen.add(thread_id)
        decision = str(raw.get("decision") or "").strip()
        if decision == "ignore":
            reason = str(raw.get("reason_code") or "model_ignore").strip()
            output.append(
                GmailThreadDetection(
                    thread_id=thread_id,
                    disposition="suppressed",
                    reason_code=reason[:128] or "model_ignore",
                    confidence=_confidence(raw.get("confidence"), default=0.7),
                )
            )
            continue
        if decision != "candidates":
            raise GmailDetectorError("detector decision must be ignore or candidates")
        raw_candidates = raw.get("candidates")
        if not isinstance(raw_candidates, list) or not raw_candidates:
            raise GmailDetectorError("candidate decision requires candidates")
        candidates = tuple(
            _parse_candidate(candidate, allowed[thread_id])
            for candidate in raw_candidates[:12]
        )
        output.append(
            GmailThreadDetection(
                thread_id=thread_id,
                disposition="surfaced",
                reason_code="operational_candidate",
                candidates=candidates,
                confidence=max(candidate.confidence for candidate in candidates),
            )
        )
    missing = set(allowed) - seen
    if missing:
        raise GmailDetectorError(
            "detector omitted thread results: " + ", ".join(sorted(missing))
        )
    return tuple(output)


def _high_consequence_fallback(
    thread: NormalizedGmailThread,
    *,
    reason_code: str,
) -> GmailThreadDetection:
    message = thread.messages[-1]
    title = (thread.subject or "Review high-consequence email")[:500]
    candidate = GmailOperationalCandidate(
        detector_key=_fallback_detector_key("attention", title),
        operation="needs_reconciliation",
        kind="attention",
        title=title,
        owner="unknown",
        confidence=0.25,
        priority=90,
        evidence_message_ids=(message.message_id,),
        handled_verdict="unknown",
        handled_confidence=0.0,
        reason="A high-consequence signal could not be safely dismissed.",
        reconciliation_status="ambiguous",
    )
    return GmailThreadDetection(
        thread_id=thread.thread_id,
        disposition="surfaced",
        reason_code=reason_code,
        candidates=(candidate,),
        confidence=0.25,
    )


def _parse_candidate(
    raw: Any,
    thread: NormalizedGmailThread,
) -> GmailOperationalCandidate:
    if not isinstance(raw, Mapping):
        raise GmailDetectorError("candidate must be an object")
    operation = str(raw.get("operation") or "create").strip()
    kind = str(raw.get("kind") or "").strip()
    owner = str(raw.get("owner") or "unknown").strip()
    handled = str(raw.get("handled_verdict") or "unknown").strip()
    if operation not in OPERATIONS or kind not in ITEM_KINDS or owner not in ITEM_OWNERS:
        raise GmailDetectorError("candidate has an unsupported operation/kind/owner")
    if handled not in HANDLED_VERDICTS:
        raise GmailDetectorError("candidate has an unsupported handled verdict")
    title = _bounded_required(raw.get("title"), "candidate title", 500)
    detector_key = str(raw.get("detector_key") or "").strip()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,79}", detector_key):
        detector_key = _fallback_detector_key(kind, title)
    message_ids = tuple(
        dict.fromkeys(
            str(value).strip()
            for value in (raw.get("evidence_message_ids") or [])
            if str(value).strip()
        )
    )
    known_message_ids = {message.message_id for message in thread.messages}
    if not message_ids or not set(message_ids) <= known_message_ids:
        raise GmailDetectorError("candidate evidence must reference messages in its thread")
    reconciliation_status = str(
        raw.get("reconciliation_status") or "confirmed"
    ).strip()
    if reconciliation_status not in {"confirmed", "provisional", "ambiguous"}:
        raise GmailDetectorError("candidate has invalid reconciliation status")
    return GmailOperationalCandidate(
        detector_key=detector_key,
        operation=operation,
        kind=kind,
        title=title,
        owner=owner,
        confidence=_confidence(raw.get("confidence"), default=0.5),
        priority=_priority(raw.get("priority")),
        evidence_message_ids=message_ids,
        handled_verdict=handled,
        handled_confidence=_confidence(
            raw.get("handled_confidence"),
            default=0.0,
        ),
        due_at=_timestamp_or_none(raw.get("due_at")),
        starts_at=_timestamp_or_none(raw.get("starts_at")),
        ends_at=_timestamp_or_none(raw.get("ends_at")),
        expires_at=_timestamp_or_none(raw.get("expires_at")),
        counterparty=_bounded_optional(raw.get("counterparty"), 500),
        reason=_bounded_optional(raw.get("reason"), 1000),
        reconciliation_status=reconciliation_status,
    )


def _detector_prompt(
    threads: Sequence[NormalizedGmailThread],
    *,
    operator_emails: Sequence[str],
    timezone_name: str,
    policy_version: str,
    active_items_by_thread: Mapping[str, Sequence[Mapping[str, Any]]],
) -> str:
    payload = [
        _thread_model_payload(
            thread,
            active_items=active_items_by_thread.get(thread.thread_id, ()),
        )
        for thread in threads
    ]
    return (
        "You are a read-only operational email detector for one operator. Analyze each "
        "thread exactly once. Find current commitments, waiting items, follow-ups, "
        "deadlines, or attention/decision items. Bulk and transactional mail can contain "
        "important logistics, payments, renewals, travel changes, deliveries, appointments, "
        "or security deadlines; do not reject it merely because it is automated. Marketing "
        "with no current action should be ignored.\n\n"
        "Handled-state rules: read/unread never proves completion; an outgoing reply proves "
        "only response unless it directly supplies or explicitly declines the requested "
        "result; a promise to act remains needs_action; silence never means being handled; "
        "being_handled requires an identified other owner and direct progress evidence; if "
        "coverage or meaning is ambiguous use unknown. Do not invent dates, owners, customers, "
        "or completion. Dates must be explicit in an evidence message and returned as ISO-8601 "
        "with timezone; otherwise null. Reuse an active item's detector_key when it is the same "
        "obligation. Use a short stable lowercase detector_key. False merges are worse than "
        "duplicates: use needs_reconciliation plus ambiguous when unsure.\n\n"
        "Return one result for every input thread. Shape: "
        "{\"threads\":[{\"thread_id\":str,\"decision\":\"ignore|candidates\","
        "\"reason_code\":str,\"confidence\":0..1,\"candidates\":[{"
        "\"detector_key\":str,\"operation\":\"create|update|resolve|cancel|none|needs_reconciliation\","
        "\"kind\":\"commitment|waiting|follow_up|deadline|attention\","
        "\"title\":str,\"owner\":\"operator|other|shared|unknown\","
        "\"priority\":\"critical|high|normal|low|awareness\",\"confidence\":0..1,"
        "\"due_at\":str|null,\"starts_at\":str|null,\"ends_at\":str|null,"
        "\"expires_at\":str|null,\"counterparty\":str|null,"
        "\"evidence_message_ids\":[str],\"handled_verdict\":"
        "\"needs_action|responded_waiting|being_handled|fulfilled|unknown\","
        "\"handled_confidence\":0..1,\"reason\":str,\"reconciliation_status\":"
        "\"confirmed|provisional|ambiguous\"}]}]}.\n\n"
        f"Operator emails: {json.dumps(list(operator_emails))}\n"
        f"Timezone: {timezone_name}\nPolicy: {policy_version}\n"
        f"Threads: {json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}"
    )


def _thread_model_payload(
    thread: NormalizedGmailThread,
    *,
    active_items: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    messages = [_message_payload(message) for message in thread.messages]
    return {
        "thread_id": thread.thread_id,
        "subject": thread.subject,
        "message_class": thread.message_class,
        "created_at": thread.created_at,
        "updated_at": thread.updated_at,
        "messages": messages,
        "active_items": [
            {
                "detector_key": str(item.get("detector_key") or "")[:80],
                "kind": str(item.get("kind") or item.get("item_kind") or "")[:40],
                "title": str(item.get("title") or "")[:500],
                "owner": str(item.get("owner") or "unknown")[:40],
                "due_at": item.get("due_at"),
                "state": str(item.get("state") or "active")[:40],
            }
            for item in active_items[:20]
        ],
    }


def _message_payload(message: NormalizedGmailMessage) -> dict[str, Any]:
    return {
        "message_id": message.message_id,
        "timestamp": message.timestamp,
        "from": list(message.from_addresses),
        "to": list(message.to_addresses),
        "cc": list(message.cc_addresses),
        "outgoing": message.outgoing,
        "operator_authored": message.operator_authored,
        "body": message.body,
    }


def _thread_signal_text(thread: NormalizedGmailThread) -> str:
    return "\n".join(
        [thread.subject or "", *(message.body for message in thread.messages)]
    )


def _estimate_tokens(value: Any) -> int:
    encoded = (
        value
        if isinstance(value, str)
        else json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    )
    return max(1, (len(encoded) + 3) // 4)


def _priority(value: Any) -> int:
    if isinstance(value, bool):
        raise GmailDetectorError("priority cannot be boolean")
    if isinstance(value, int):
        return max(-100, min(100, value))
    normalized = str(value or "normal").strip().casefold()
    if normalized not in _PRIORITY_WORDS:
        raise GmailDetectorError("candidate priority is invalid")
    return _PRIORITY_WORDS[normalized]


def _confidence(value: Any, *, default: float) -> float:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise GmailDetectorError("confidence must be numeric")
    numeric = float(value)
    if not 0.0 <= numeric <= 1.0:
        raise GmailDetectorError("confidence must be between 0 and 1")
    return numeric


def _timestamp_or_none(value: Any) -> str | None:
    if value is None or not str(value).strip():
        return None
    text = str(value).strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise GmailDetectorError("candidate timestamp must be ISO-8601") from exc
    if parsed.tzinfo is None:
        raise GmailDetectorError("candidate timestamp must include a timezone")
    return parsed.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def _fallback_detector_key(kind: str, title: str) -> str:
    digest = hashlib.sha256(f"{kind}\0{title.casefold()}".encode("utf-8")).hexdigest()
    return f"{kind}-{digest[:16]}"


def _bounded_required(value: Any, label: str, maximum: int) -> str:
    normalized = str(value or "").strip()
    if not normalized or len(normalized) > maximum:
        raise GmailDetectorError(f"{label} must be 1-{maximum} characters")
    return normalized


def _bounded_optional(value: Any, maximum: int) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    if not normalized:
        return None
    if len(normalized) > maximum:
        raise GmailDetectorError("candidate optional text is too long")
    return normalized
