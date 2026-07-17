from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .fact_records import effective_revision_status


TEMPORAL_KINDS = {
    "atemporal",
    "instantaneous",
    "time_bound",
    "ongoing",
    "unknown",
}
VALID_TIME_PRECISIONS = {
    "exact",
    "day",
    "month",
    "year",
    "approximate",
    "unknown",
}
EVENT_TIME_KINDS = {"actual", "planned"}
EVENT_TIME_PRECISIONS = {"exact", "day", "month", "year"}
TEMPORAL_RETRIEVAL_MODES = {
    "bitemporal",
    "current",
    "known",
    "timeline",
    "valid",
}

_PARTIAL_DATE_RE = re.compile(
    r"^(?P<year>\d{4})(?:-(?P<month>0[1-9]|1[0-2]))?(?:-(?P<day>0[1-9]|[12]\d|3[01]))?$"
)
_AS_OF_RE = re.compile(
    r"\b(?P<cue>as\s+of|on)\s+"
    r"(?P<value>\d{4}(?:-\d{2}(?:-\d{2})?)?(?:[T ][0-9:.+\-Zz]+)?)\b",
    re.IGNORECASE,
)
_KNOWLEDGE_INTENT_RE = re.compile(
    r"\b(?:"
    r"(?:do|did)\s+(?:we|brain|the\s+system)\s+(?:know|believe)|"
    r"(?:we|brain|the\s+system)\s+(?:know|knew|believe|believed)|"
    r"(?:is|are|was|were)\s+(?:known|believed)|"
    r"(?:our|brain(?:'s)?|the\s+system(?:'s)?)\s+(?:knowledge|beliefs?)"
    r")\b",
    re.IGNORECASE,
)

_CURRENT_MATCH_LABELS = {
    "atemporal",
    "current",
    "legacy_unknown",
    "ongoing_unknown_start",
    "unknown_valid_time",
}

_EVENT_TIME_FLAT_KEYS = (
    "event_time_kind",
    "event_start_at",
    "event_end_at",
    "event_time_precision",
    "event_time_expression",
)


@dataclass(frozen=True)
class TemporalRetrievalRequest:
    valid_as_of: str | None = None
    known_as_of: str | None = None
    event_as_of: str | None = None
    event_kind: str | None = None
    temporal_mode: str = "current"
    valid_inferred: bool = False
    known_inferred: bool = False
    warning: str | None = None

    @classmethod
    def resolve(
        cls,
        task: str,
        valid_as_of: str | None = None,
        *,
        known_as_of: str | None = None,
        event_as_of: str | None = None,
        event_kind: str | None = None,
        temporal_mode: str | None = None,
    ) -> "TemporalRetrievalRequest":
        """Resolve explicit clock inputs before conservative task inference."""

        mode = normalize_temporal_mode(temporal_mode)
        valid = normalize_explicit_as_of("valid_as_of", valid_as_of)
        known = normalize_explicit_as_of("known_as_of", known_as_of)
        event = normalize_explicit_event_as_of(event_as_of)
        normalized_event_kind = normalize_event_kind(event_kind)
        valid_inferred = False
        known_inferred = False
        warning: str | None = None

        # A supplied clock is authoritative. Do not infer a second clock from prose.
        if valid is None and known is None and event is None and mode != "current":
            match = _AS_OF_RE.search(task)
            if match:
                inferred = normalize_as_of_value(match.group("value"))
                if inferred is None:
                    warning = "ambiguous temporal expression was not applied"
                elif mode == "known" or (
                    mode in {None, "timeline"}
                    and is_knowledge_as_of_request(task, match)
                ):
                    known = inferred
                    known_inferred = True
                elif mode in {None, "valid", "timeline"}:
                    valid = inferred
                    valid_inferred = True

        if mode is None:
            if valid and known:
                mode = "bitemporal"
            elif valid:
                mode = "valid"
            elif known:
                mode = "known"
            else:
                mode = "current"

        validate_temporal_request(
            mode,
            valid,
            known,
            event,
            normalized_event_kind,
            warning,
        )
        return cls(
            valid_as_of=valid,
            known_as_of=known,
            event_as_of=event,
            event_kind=normalized_event_kind,
            temporal_mode=mode,
            valid_inferred=valid_inferred,
            known_inferred=known_inferred,
            warning=warning,
        )

    @property
    def inferred(self) -> bool:
        return self.valid_inferred or self.known_inferred

    @property
    def historical(self) -> bool:
        return self.temporal_mode != "current" or self.event_as_of is not None

    @property
    def fail_closed(self) -> bool:
        return self.warning is not None

    @property
    def include_current_layers(self) -> bool:
        return not self.historical and not self.fail_closed

    @property
    def include_supporting_chunks(self) -> bool:
        """Avoid leaking later source material into knowledge-time answers."""

        return not self.fail_closed and self.temporal_mode not in {
            "bitemporal",
            "known",
        }

    @property
    def mode(self) -> str:
        return self.temporal_mode

    def debug(self) -> dict[str, Any]:
        payload = {
            "mode": self.mode,
            "valid_as_of": self.valid_as_of,
            "known_as_of": self.known_as_of,
            "inferred": self.inferred,
            "valid_inferred": self.valid_inferred,
            "known_inferred": self.known_inferred,
            "warning": self.warning,
        }
        if self.event_as_of is not None:
            payload["event_as_of"] = self.event_as_of
            payload["event_kind"] = self.event_kind
        return payload

    def envelope(self) -> dict[str, Any]:
        if self.fail_closed:
            omitted = [
                "facts",
                "wiki_pages",
                "memories",
                "supporting_chunks",
                "open_questions",
            ]
            coverage = "unavailable"
        elif self.historical:
            omitted = ["wiki_pages", "memories", "open_questions"]
            if not self.include_supporting_chunks:
                omitted.append("supporting_chunks")
            coverage = {
                "bitemporal": "explicit_bitemporal",
                "current": "explicit_event_time",
                "known": "explicit_knowledge_time_only",
                "timeline": "timeline",
                "valid": "explicit_valid_time_only",
            }[self.temporal_mode]
            if self.event_as_of is not None and self.temporal_mode != "current":
                coverage = f"{coverage}_with_event_time"
        else:
            omitted = []
            coverage = "current_valid_time_with_legacy_unknown"
        return {
            **self.debug(),
            "fact_coverage": coverage,
            "omitted_current_state_layers": omitted,
        }


def normalize_temporal_mode(value: str | None) -> str | None:
    if value is None:
        return None
    mode = str(value).strip().lower()
    if mode not in TEMPORAL_RETRIEVAL_MODES:
        choices = "|".join(sorted(TEMPORAL_RETRIEVAL_MODES))
        raise ValueError(f"temporal_mode must be one of {choices}")
    return mode


def normalize_event_kind(value: str | None) -> str | None:
    if value is None:
        return None
    kind = str(value).strip().lower()
    if kind not in EVENT_TIME_KINDS:
        choices = "|".join(sorted(EVENT_TIME_KINDS))
        raise ValueError(f"event_kind must be one of {choices}")
    return kind


def normalize_explicit_as_of(name: str, value: str | None) -> str | None:
    if value is None:
        return None
    normalized = normalize_as_of_value(value)
    if normalized is None:
        raise ValueError(f"{name} must be an ISO-8601 year, month, date, or timestamp")
    return normalized


def normalize_explicit_event_as_of(value: str | None) -> str | None:
    """Validate event time while preserving a partial date's query interval."""

    if value is None:
        return None
    text = optional_text(value)
    start = parse_temporal_value(text, boundary="start")
    end = parse_temporal_value(text, boundary="end")
    if text is None or start is None or end is None:
        raise ValueError(
            "event_as_of must be an ISO-8601 year, month, date, or timestamp"
        )
    if _PARTIAL_DATE_RE.fullmatch(text):
        return text
    return start.isoformat()


def validate_temporal_request(
    mode: str,
    valid_as_of: str | None,
    known_as_of: str | None,
    event_as_of: str | None,
    event_kind: str | None,
    warning: str | None,
) -> None:
    if warning is not None:
        return
    if mode == "current" and (valid_as_of or known_as_of):
        raise ValueError("current temporal_mode does not accept as-of clocks")
    if mode == "valid" and known_as_of is not None:
        raise ValueError("valid temporal_mode does not accept known_as_of")
    if mode == "known" and valid_as_of is not None:
        raise ValueError("known temporal_mode does not accept valid_as_of")
    if mode == "valid" and valid_as_of is None:
        raise ValueError("valid temporal_mode requires valid_as_of")
    if mode == "known" and known_as_of is None:
        raise ValueError("known temporal_mode requires known_as_of")
    if mode == "bitemporal" and not (valid_as_of and known_as_of):
        raise ValueError(
            "bitemporal temporal_mode requires valid_as_of and known_as_of"
        )
    if event_kind is not None and event_as_of is None:
        raise ValueError("event_kind requires event_as_of")


def normalize_temporal_candidate(
    candidate: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    """Normalize extractor temporal fields without inventing missing dates."""

    normalized = dict(candidate)
    errors: list[str] = []
    kind = str(candidate.get("temporal_kind") or "unknown").strip().lower()
    if kind not in TEMPORAL_KINDS:
        errors.append(f"invalid temporal_kind: {kind}")
        kind = "unknown"

    valid_from = optional_text(
        candidate.get("valid_from") or candidate.get("effective_at")
    )
    valid_to = optional_text(candidate.get("valid_to"))
    precision = str(candidate.get("valid_time_precision") or "unknown").strip().lower()
    if precision not in VALID_TIME_PRECISIONS:
        errors.append(f"invalid valid_time_precision: {precision}")
        precision = "unknown"
    precision = normalized_precision(precision, valid_from, valid_to)

    confidence = optional_float(candidate.get("temporal_confidence"))
    raw_confidence = candidate.get("temporal_confidence")
    if raw_confidence is not None and raw_confidence != "" and confidence is None:
        errors.append("temporal_confidence must be numeric")
    elif confidence is not None and not 0.0 <= confidence <= 1.0:
        errors.append("temporal_confidence must be between 0 and 1")
        confidence = None

    start = parse_temporal_value(valid_from, boundary="start") if valid_from else None
    end = parse_temporal_value(valid_to, boundary="start") if valid_to else None
    if valid_from and start is None:
        errors.append("valid_from must be ISO-8601")
    if valid_to and end is None:
        errors.append("valid_to must be ISO-8601")
    if start is not None and end is not None and end <= start:
        errors.append("valid_to must be later than valid_from")

    if kind == "atemporal" and (valid_from or valid_to):
        errors.append("atemporal facts cannot have valid_from or valid_to")
    if kind == "instantaneous" and not valid_from:
        errors.append("instantaneous facts require valid_from")
    if kind == "instantaneous" and valid_to:
        errors.append("instantaneous facts cannot have valid_to")
    if kind == "time_bound" and (not valid_from or not valid_to):
        errors.append("time_bound facts require valid_from and valid_to")
    if kind == "ongoing" and valid_to:
        errors.append("ongoing facts cannot have valid_to")
    if kind == "unknown" and (valid_from or valid_to):
        errors.append("unknown facts cannot have normalized valid bounds")

    normalized.update(
        {
            # effective_at remains a compatibility mirror for older readers.
            "effective_at": valid_from,
            "valid_from": valid_from,
            "valid_to": valid_to,
            "temporal_kind": kind,
            "valid_time_precision": precision,
            "temporal_expression": optional_text(candidate.get("temporal_expression")),
            "temporal_confidence": confidence,
        }
    )
    return normalized, errors


def normalize_event_time_candidate(
    candidate: dict[str, Any],
    *,
    primary_entity_is_event: bool,
) -> tuple[dict[str, Any], list[str]]:
    """Validate optional event timing without making it a fact-acceptance gate.

    The returned candidate always retains the base assertion. A missing or
    malformed enrichment is represented as ``event_time=None`` with cleared
    flat persistence fields, while ``errors`` lets extraction report why the
    enrichment was omitted. Callers must pass an explicit primary-entity
    decision so event timing cannot silently attach to a non-event entity.
    """

    normalized = dict(candidate)
    raw = candidate.get("event_time")
    if raw is None:
        return _without_event_time(normalized), []

    errors: list[str] = []
    if not isinstance(raw, dict):
        return _without_event_time(normalized), ["event_time must be an object"]
    if not primary_entity_is_event:
        errors.append("event_time requires the primary entity to be an event")

    kind = str(raw.get("kind") or "").strip().lower()
    if kind not in EVENT_TIME_KINDS:
        errors.append("event_time.kind must be actual or planned")

    start_at = optional_text(raw.get("start_at"))
    end_at = optional_text(raw.get("end_at"))
    if start_at is None and end_at is None:
        errors.append("event_time requires start_at or end_at")
    if kind == "actual" and start_at is None:
        errors.append("actual event_time requires start_at")

    start = parse_temporal_value(start_at, boundary="start") if start_at else None
    end = parse_temporal_value(end_at, boundary="start") if end_at else None
    if start_at and start is None:
        errors.append("event_time.start_at must be ISO-8601")
    if end_at and end is None:
        errors.append("event_time.end_at must be ISO-8601")
    if start is not None and end is not None and end <= start:
        errors.append("event_time.end_at must be later than start_at")

    precision = str(raw.get("precision") or "").strip().lower()
    if precision not in EVENT_TIME_PRECISIONS:
        choices = "|".join(sorted(EVENT_TIME_PRECISIONS))
        errors.append(f"event_time.precision must be one of {choices}")
    derived_precision = event_bound_precision(start_at, end_at)
    if precision in EVENT_TIME_PRECISIONS and derived_precision != precision:
        errors.append(
            "event_time.precision does not match the supplied start/end bounds"
        )

    expression = optional_text(raw.get("expression"))
    if errors:
        return _without_event_time(normalized), errors

    # Exact timestamps have one storage/signature form.  This keeps equivalent
    # UTC spellings (``Z``, ``+00:00``, or another explicit offset) from
    # producing different event identities while preserving partial-date
    # precision verbatim.
    start_at = canonical_event_bound(start_at)
    end_at = canonical_event_bound(end_at)

    event_time = {
        "kind": kind,
        "start_at": start_at,
        "end_at": end_at,
        "precision": precision,
        "expression": expression,
    }
    normalized.update(
        {
            "event_time": event_time,
            "event_time_kind": kind,
            "event_start_at": start_at,
            "event_end_at": end_at,
            "event_time_precision": precision,
            "event_time_expression": expression,
        }
    )
    return normalized, []


def event_bound_precision(start_at: str | None, end_at: str | None) -> str | None:
    """Return the coarsest ISO precision carried by the supplied event bounds."""

    ranks = {"year": 0, "month": 1, "day": 2, "exact": 3}
    precisions = [
        precision
        for value in (start_at, end_at)
        if value and (precision := temporal_value_precision(value)) is not None
    ]
    if not precisions:
        return None
    return min(precisions, key=ranks.__getitem__)


def temporal_value_precision(value: str) -> str | None:
    text = str(value or "").strip()
    if re.fullmatch(r"\d{4}", text):
        return "year"
    if re.fullmatch(r"\d{4}-\d{2}", text):
        return "month"
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return "day"
    if "T" in text or re.fullmatch(r"\d{4}-\d{2}-\d{2}\s+.+", text):
        return "exact"
    return None


def canonical_event_bound(value: str | None) -> str | None:
    """Canonicalize an exact event timestamp to UTC; preserve partial dates."""

    text = optional_text(value)
    if text is None or temporal_value_precision(text) != "exact":
        return text
    parsed = parse_temporal_value(text, boundary="start")
    return parsed.isoformat() if parsed is not None else text


def _without_event_time(candidate: dict[str, Any]) -> dict[str, Any]:
    candidate["event_time"] = None
    candidate.update({key: None for key in _EVENT_TIME_FLAT_KEYS})
    return candidate


def temporal_grounding_errors(
    candidate: dict[str, Any], evidence_text: str
) -> list[str]:
    expression = optional_text(candidate.get("temporal_expression"))
    has_normalized_time = bool(candidate.get("valid_from") or candidate.get("valid_to"))
    if has_normalized_time and not expression:
        return ["normalized valid time requires temporal_expression"]
    if not expression:
        return []
    normalized_expression = " ".join(expression.casefold().split())
    normalized_evidence = " ".join(str(evidence_text).casefold().split())
    if normalized_expression not in normalized_evidence:
        return ["temporal_expression is not present in cited evidence"]
    return []


def event_time_grounding_errors(
    candidate: dict[str, Any], evidence_text: str
) -> list[str]:
    """Require an optional event-time expression to be literal evidence text."""

    fields = _event_time_fields(candidate)
    expression = fields[4]
    if not expression:
        return []
    normalized_expression = " ".join(expression.casefold().split())
    normalized_evidence = " ".join(str(evidence_text).casefold().split())
    if normalized_expression not in normalized_evidence:
        return ["event_time.expression is not present in cited evidence"]
    return []


def is_knowledge_as_of_request(task: str, match: re.Match[str]) -> bool:
    """Recognize epistemic `as of`, never a fact/event date introduced by `on`."""

    if match.group("cue").casefold().replace(" ", "") != "asof":
        return False
    prefix = task[max(0, match.start() - 120) : match.start()]
    return _KNOWLEDGE_INTENT_RE.search(prefix) is not None


def normalize_as_of_value(value: str) -> str | None:
    parsed = parse_temporal_value(value, boundary="end")
    return parsed.isoformat() if parsed is not None else None


def fact_matches_valid_time(
    fact: dict[str, Any],
    valid_as_of: str,
    *,
    knowledge_scoped: bool = False,
) -> bool:
    """Match the half-open valid interval at a point.

    `knowledge_scoped` is only for a simultaneous knowledge-time predicate. It
    permits a then-known superseded assertion without guessing its later state.
    """

    point = parse_temporal_value(valid_as_of, boundary="end")
    if point is None:
        return False
    kind = str(fact.get("temporal_kind") or "unknown").lower()
    if kind not in TEMPORAL_KINDS:
        return False
    status = (
        effective_revision_status(fact)
        if knowledge_scoped
        else str(fact.get("status") or "")
    )
    if status not in {"active", "conflicted", "superseded"}:
        return False
    valid_from = optional_text(fact.get("valid_from"))
    valid_to = optional_text(fact.get("valid_to"))
    start = parse_temporal_value(valid_from, boundary="start") if valid_from else None
    end = parse_temporal_value(valid_to, boundary="start") if valid_to else None
    if valid_from and start is None or valid_to and end is None:
        return False

    if kind == "atemporal":
        if valid_from or valid_to:
            return False
        return knowledge_scoped or status in {"active", "conflicted"}
    if kind == "instantaneous":
        if start is None or end is not None:
            return False
        precision = temporal_precision(fact)
        if precision in {"approximate", "unknown"}:
            precision = infer_precision(valid_from)
        instant_end = precision_end(start, precision)
        return start <= point < instant_end
    if kind == "unknown":
        return False
    if kind == "time_bound" and (start is None or end is None):
        return False
    if kind == "ongoing" and (start is None or end is not None):
        return False
    if point < start or (end is not None and point >= end):
        return False
    if status == "superseded" and end is None and not knowledge_scoped:
        return False
    return status in {"active", "conflicted", "superseded"}


def fact_matches_knowledge_time(fact: dict[str, Any], known_as_of: str) -> bool:
    status = effective_revision_status(fact)
    if status and status not in {"active", "conflicted", "superseded"}:
        return False
    point = parse_temporal_value(known_as_of, boundary="end")
    created_text = optional_text(fact.get("created_at"))
    knowledge_to_text = optional_text(fact.get("knowledge_to"))
    created = parse_temporal_value(created_text, boundary="start")
    knowledge_to = (
        parse_temporal_value(knowledge_to_text, boundary="start")
        if knowledge_to_text
        else None
    )
    if knowledge_to_text and knowledge_to is None:
        return False
    if point is None or created is None or created > point:
        return False
    return knowledge_to is None or point < knowledge_to


def fact_matches_temporal_request(
    fact: dict[str, Any],
    request: TemporalRetrievalRequest,
    *,
    current_at: str | datetime | None = None,
) -> bool:
    """Apply exactly the clock predicates selected by a retrieval request."""

    if request.fail_closed:
        return False
    event_matches = request.event_as_of is None or fact_matches_event_time(
        fact,
        request.event_as_of,
        kind=request.event_kind,
    )
    if request.temporal_mode == "current":
        return fact_matches_current_time(fact, current_at=current_at) and event_matches

    knowledge_matches = request.known_as_of is None or fact_matches_knowledge_time(
        fact, request.known_as_of
    )
    valid_matches = request.valid_as_of is None or fact_matches_valid_time(
        fact,
        request.valid_as_of,
        knowledge_scoped=request.known_as_of is not None,
    )
    if request.temporal_mode == "timeline":
        status = str(fact.get("status") or "")
        reviewed = not status or status in {"active", "conflicted", "superseded"}
        return reviewed and knowledge_matches and valid_matches and event_matches
    return knowledge_matches and valid_matches and event_matches


def fact_matches_current_time(
    fact: dict[str, Any], *, current_at: str | datetime | None = None
) -> bool:
    return timeline_currentness(fact, current_at=current_at) in _CURRENT_MATCH_LABELS


def fact_matches_event_time(
    fact: dict[str, Any],
    value: str,
    *,
    kind: str | None = None,
) -> bool:
    """Match an explicit date/timestamp against an event's actual or planned time.

    Event time is an orthogonal retrieval facet: this helper is opt-in and is
    deliberately not used by current fact retrieval. Partial query dates are
    treated as intervals, allowing an exact event timestamp to match its day.
    """

    event_kind, start_text, end_text, precision, _expression = _event_time_fields(fact)
    if event_kind not in EVENT_TIME_KINDS or precision not in EVENT_TIME_PRECISIONS:
        return False
    requested_kind = str(kind or "").strip().lower()
    if requested_kind and requested_kind not in EVENT_TIME_KINDS:
        return False
    if requested_kind and requested_kind != event_kind:
        return False

    start = parse_temporal_value(start_text, boundary="start") if start_text else None
    end = parse_temporal_value(end_text, boundary="start") if end_text else None
    if start_text and start is None or end_text and end is None:
        return False
    if start is None and end is None:
        return False
    if start is not None and end is not None and end <= start:
        return False

    if start is None:
        event_start = end
        event_end = precision_end(end, precision) if end is not None else None
    else:
        event_start = start
        event_end = end or precision_end(start, precision)
    if event_start is None or event_end is None:
        return False

    query_start = parse_temporal_value(value, boundary="start")
    query_last = parse_temporal_value(value, boundary="end")
    if query_start is None or query_last is None:
        return False
    query_end = query_last + timedelta(microseconds=1)
    return event_start < query_end and query_start < event_end


def timeline_currentness(
    fact: dict[str, Any], *, current_at: str | datetime | None = None
) -> str:
    """Label a timeline row without hiding active legacy unknown-time facts."""

    status = str(fact.get("status") or "active").strip().lower()
    if status == "superseded":
        return "superseded"
    if status not in {"active", "conflicted"}:
        return "unreviewed"

    point = current_time_point(current_at)
    if point is None:
        return "invalid_current_time"
    kind = str(fact.get("temporal_kind") or "unknown").strip().lower()
    if kind not in TEMPORAL_KINDS:
        return "invalid_temporal_kind"
    if kind == "atemporal":
        return "atemporal"
    if kind == "unknown":
        # Migration leaves older unclassified rows at NULL. Preserve those in
        # current retrieval for compatibility. Explicitly classified unknown
        # rows are also retained: unknown validity must not become a reason to
        # hide an otherwise active fact from the default snapshot.
        if fact.get("temporal_confidence") is None:
            return "legacy_unknown"
        return "unknown_valid_time"

    valid_from = optional_text(fact.get("valid_from")) or optional_text(
        fact.get("effective_at")
    )
    valid_to = optional_text(fact.get("valid_to"))
    start = parse_temporal_value(valid_from, boundary="start") if valid_from else None
    end = parse_temporal_value(valid_to, boundary="start") if valid_to else None
    if valid_from and start is None or valid_to and end is None:
        return "invalid_valid_time"

    if kind == "ongoing" and start is None and end is None:
        return "ongoing_unknown_start"
    if kind == "instantaneous" and start is not None:
        precision = temporal_precision(fact)
        if precision in {"approximate", "unknown"}:
            precision = infer_precision(valid_from)
        end = precision_end(start, precision)
    elif kind == "time_bound" and (start is None or end is None):
        return "unknown_bounds"
    elif kind == "ongoing" and end is not None:
        return "invalid_valid_time"
    elif start is None:
        return "unknown_bounds"
    if end is not None and end <= start:
        return "invalid_valid_time"

    if point < start:
        return "future"
    if end is not None and point >= end:
        return "historical"
    return "current"


def timeline_sort_key(fact: dict[str, Any]) -> tuple[Any, ...]:
    """Order by predicate validity, then event time; leave undated rows last."""

    valid_from = optional_text(fact.get("valid_from")) or optional_text(
        fact.get("effective_at")
    )
    start = parse_temporal_value(valid_from, boundary="start") if valid_from else None
    valid_to = optional_text(fact.get("valid_to"))
    end = parse_temporal_value(valid_to, boundary="start") if valid_to else None
    if start is None:
        _kind, event_start, event_end, _precision, _expression = _event_time_fields(
            fact
        )
        event_start = event_start or event_end
        start = (
            parse_temporal_value(event_start, boundary="start")
            if event_start
            else None
        )
        end = (
            parse_temporal_value(event_end, boundary="start") if event_end else None
        )
    created = parse_temporal_value(
        optional_text(fact.get("created_at")), boundary="start"
    )
    latest = datetime.max.replace(tzinfo=timezone.utc)
    return (
        start is None,
        start or latest,
        end or latest,
        created or latest,
        str(fact.get("id") or ""),
    )


def current_time_point(value: str | datetime | None) -> datetime | None:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return None
        return value.astimezone(timezone.utc)
    if value is None:
        return datetime.now(timezone.utc)
    return parse_temporal_value(value, boundary="end")


def temporal_signature(fact: dict[str, Any]) -> tuple[str, str, str, str]:
    """Return the assertion's temporal identity for safe duplicate handling."""

    kind = str(fact.get("temporal_kind") or "unknown").strip().lower()
    valid_from = optional_text(fact.get("valid_from")) or optional_text(
        fact.get("effective_at")
    )
    valid_to = optional_text(fact.get("valid_to"))
    precision = temporal_precision(fact)
    return (kind, valid_from or "", valid_to or "", precision)


def temporal_merge_compatible(left: dict[str, Any], right: dict[str, Any]) -> bool:
    """Only merge assertions with the same predicate and event-time identity."""

    return temporal_signature(left) == temporal_signature(
        right
    ) and event_time_signature(left) == event_time_signature(right)


def event_time_signature(fact: dict[str, Any]) -> tuple[str, str, str, str]:
    """Return the normalized event-time identity used for duplicate handling."""

    kind, start_at, end_at, precision, _expression = _event_time_fields(fact)
    return (
        kind or "",
        canonical_event_bound(start_at) or "",
        canonical_event_bound(end_at) or "",
        precision or "",
    )


def _event_time_fields(
    fact: dict[str, Any],
) -> tuple[str | None, str | None, str | None, str | None, str | None]:
    raw = fact.get("event_time")
    if isinstance(raw, dict):
        return (
            optional_text(raw.get("kind")),
            optional_text(raw.get("start_at")),
            optional_text(raw.get("end_at")),
            optional_text(raw.get("precision")),
            optional_text(raw.get("expression")),
        )
    return (
        optional_text(fact.get("event_time_kind")),
        optional_text(fact.get("event_start_at")),
        optional_text(fact.get("event_end_at")),
        optional_text(fact.get("event_time_precision")),
        optional_text(fact.get("event_time_expression")),
    )


def temporal_interval_relation(
    candidate: dict[str, Any], existing: dict[str, Any]
) -> str:
    """Compare explicit valid intervals from the candidate's direction."""

    candidate_interval = fact_interval(candidate)
    existing_interval = fact_interval(existing)
    if candidate_interval is None or existing_interval is None:
        return "unknown"
    candidate_start, candidate_end = candidate_interval
    existing_start, existing_end = existing_interval
    if candidate_end is not None and candidate_end <= existing_start:
        return "before"
    if existing_end is not None and existing_end <= candidate_start:
        return "after"
    return "overlap"


def is_safe_temporal_successor(
    candidate: dict[str, Any], facts: list[dict[str, Any]]
) -> bool:
    """Gate a resolver-selected successor on explicit non-overlapping time."""

    others = [fact for fact in facts if fact.get("id") != candidate.get("id")]
    if not others or not candidate.get("source_ids"):
        return False
    truth = float(
        candidate.get("truth_confidence") or candidate.get("confidence") or 0.0
    )
    temporal = float(candidate.get("temporal_confidence") or 0.0)
    precision = str(candidate.get("valid_time_precision") or "unknown")
    if truth < 0.8 or temporal < 0.8 or precision not in {"exact", "day"}:
        return False
    for existing in others:
        existing_truth = float(
            existing.get("truth_confidence") or existing.get("confidence") or 0.0
        )
        existing_temporal = float(existing.get("temporal_confidence") or 0.0)
        existing_precision = str(existing.get("valid_time_precision") or "unknown")
        if (
            existing_truth < 0.8
            or existing_temporal < 0.8
            or existing_precision not in {"exact", "day"}
            or not existing.get("source_ids")
            or temporal_interval_relation(candidate, existing) != "after"
        ):
            return False
    return True


def fact_interval(
    fact: dict[str, Any],
) -> tuple[datetime, datetime | None] | None:
    kind = str(fact.get("temporal_kind") or "unknown").strip().lower()
    valid_from = optional_text(fact.get("valid_from"))
    valid_to = optional_text(fact.get("valid_to"))
    start = parse_temporal_value(valid_from, boundary="start") if valid_from else None
    end = parse_temporal_value(valid_to, boundary="start") if valid_to else None
    if kind == "instantaneous" and start is not None:
        return start, precision_end(start, temporal_precision(fact))
    if kind == "time_bound" and start is not None and end is not None:
        return start, end
    if kind == "ongoing" and start is not None:
        return start, None
    return None


def temporal_precision(fact: dict[str, Any]) -> str:
    direct = str(fact.get("valid_time_precision") or "").strip().lower()
    if direct in VALID_TIME_PRECISIONS:
        return direct
    return infer_precision(optional_text(fact.get("valid_from")))


def infer_precision(value: str | None) -> str:
    if not value:
        return "unknown"
    match = _PARTIAL_DATE_RE.fullmatch(value)
    if not match:
        return "exact"
    if match.group("day"):
        return "day"
    if match.group("month"):
        return "month"
    return "year"


def normalized_precision(
    proposed: str, valid_from: str | None, valid_to: str | None
) -> str:
    if proposed == "approximate":
        return proposed
    inferred = [infer_precision(value) for value in (valid_from, valid_to) if value]
    if not inferred:
        return "unknown"
    rank = {"year": 0, "month": 1, "day": 2, "exact": 3, "unknown": 4}
    return min(inferred, key=lambda value: rank[value])


def precision_end(start: datetime, precision: str) -> datetime:
    if precision == "year":
        return start.replace(year=start.year + 1, month=1, day=1)
    if precision == "month":
        if start.month == 12:
            return start.replace(year=start.year + 1, month=1, day=1)
        return start.replace(month=start.month + 1, day=1)
    if precision == "day":
        return start + timedelta(days=1)
    return start + timedelta(microseconds=1)


def parse_temporal_value(value: str | None, *, boundary: str) -> datetime | None:
    text = optional_text(value)
    if not text:
        return None
    partial = _PARTIAL_DATE_RE.fullmatch(text)
    if partial:
        year = int(partial.group("year"))
        month = int(partial.group("month") or (12 if boundary == "end" else 1))
        if partial.group("day"):
            day = int(partial.group("day"))
        elif boundary == "end":
            day = calendar.monthrange(year, month)[1]
        else:
            day = 1
        hour, minute, second, microsecond = (
            (23, 59, 59, 999999) if boundary == "end" else (0, 0, 0, 0)
        )
        try:
            return datetime(
                year,
                month,
                day,
                hour,
                minute,
                second,
                microsecond,
                tzinfo=timezone.utc,
            )
        except ValueError:
            return None
    try:
        parsed = datetime.fromisoformat(
            text.replace("Z", "+00:00").replace("z", "+00:00")
        )
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def optional_text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
