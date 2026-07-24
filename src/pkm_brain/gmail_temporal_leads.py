from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Literal, TypeVar
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .gmail_temporal_discovery import (
    GMAIL_TEMPORAL_ACTION_VERBS,
    discover_gmail_temporal_candidates,
    gmail_temporal_event_head_is_artifact_object,
)


TemporalRelation = Literal["occurrence", "deadline"]
TemporalKind = Literal["planned", "actual"]
TemporalField = Literal["subject", "body", "message"]
GMAIL_TEMPORAL_LEAD_POLICY_VERSION = "gmail_temporal_lead_policy_v8"
AssociationAdmissionBasis = Literal["none", "fact", "temporal_rescue"]
ResolutionStatus = Literal["resolved", "ambiguous", "unresolved"]
AssociationMode = Literal[
    "direct_grammar",
    "field_local",
    "field_near",
    "subject_body_bridge",
    "subject_singleton",
    "message_singleton",
]
ConfidenceTier = Literal[
    "strict_direct",
    "review_resolved",
    "review_fallback",
    "review_ambiguous",
]


@dataclass(frozen=True)
class TemporalLocalTime:
    """Typed wall-clock components retained without pretending they are an instant."""

    hour_options: tuple[int, ...]
    minute: int
    second: int
    microsecond: int
    timezone_basis: str | None
    utc_offset_minutes: int | None
    zone_identifier: str | None


@dataclass(frozen=True)
class TemporalExpression:
    """Content-free temporal expression inventory entry.

    Offsets are exact half-open offsets into the caller-provided message.  Source
    text is deliberately absent.  More than one normalized option is preserved
    as ambiguity and is never resolved by choosing a locale or weekday convention.
    """

    expression_id: str
    start: int
    end: int
    field: TemporalField
    segment_id: str
    form: str
    normalized_options: tuple[str, ...]
    calendar_date_options: tuple[str, ...]
    local_time: TemporalLocalTime | None
    precision: str | None
    resolution_status: ResolutionStatus
    resolution_basis: tuple[str, ...]
    blockers: tuple[str, ...] = ()


@dataclass(frozen=True)
class TemporalMention:
    """Content-free event, action, lifecycle, or artifact mention entry."""

    mention_id: str
    start: int
    end: int
    field: TemporalField
    segment_id: str
    mention_type: str
    relation: TemporalRelation | None
    kind: TemporalKind | None
    boundary_role: str | None
    lifecycle_role: str | None
    blockers: tuple[str, ...] = ()


@dataclass(frozen=True)
class TemporalLead:
    """A review lead only; it is never a fact, route, or persistence action."""

    lead_id: str
    expression_id: str
    mention_id: str
    association_mode: AssociationMode
    relation: TemporalRelation | None
    kind: TemporalKind | None
    confidence_tier: ConfidenceTier
    gap_chars: int
    blockers: tuple[str, ...] = ()
    risk_features: tuple[str, ...] = ()
    routable: Literal[False] = False


@dataclass(frozen=True)
class TemporalLeadAnalysis:
    """Immutable, content-free result for one trusted Gmail message."""

    version: Literal["gmail_temporal_leads_v2"]
    snapshot_fingerprint: str
    source_sha256: str
    scope_bound: bool
    fact_admitted: bool
    association_admission_basis: AssociationAdmissionBasis
    expressions: tuple[TemporalExpression, ...]
    mentions: tuple[TemporalMention, ...]
    leads: tuple[TemporalLead, ...]
    candidate_edge_count: int
    candidate_edge_count_exact: bool
    omitted_expression_count: int
    omitted_mention_count: int
    retained_edge_count: int
    graph_truncated: bool


@dataclass(frozen=True)
class _FieldRange:
    name: TemporalField
    start: int
    end: int


@dataclass(frozen=True)
class _SegmentRange:
    segment_id: str
    field: TemporalField
    start: int
    end: int


@dataclass(frozen=True)
class _ExpressionDraft:
    start: int
    end: int
    form: str
    normalized_options: tuple[str, ...]
    precision: str | None
    resolution_basis: tuple[str, ...]
    blockers: tuple[str, ...] = ()
    calendar_date_options: tuple[str, ...] = ()
    local_time: TemporalLocalTime | None = None


@dataclass(frozen=True)
class _MentionDraft:
    start: int
    end: int
    mention_type: str
    relation: TemporalRelation | None
    kind: TemporalKind | None
    boundary_role: str | None
    lifecycle_role: str | None
    blockers: tuple[str, ...] = ()


_MONTHS = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}
_MONTH_PATTERN = "|".join(sorted(_MONTHS, key=len, reverse=True))
_WEEKDAYS = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}
_WEEKDAY_PATTERN = "|".join(_WEEKDAYS)

_ISO_DATE_RE = re.compile(
    r"(?<![\d-])(?P<year>\d{4})-(?P<month>\d{2})-(?P<day>\d{2})(?![\d-])"
)
_MONTH_DAY_YEAR_RE = re.compile(
    rf"\b(?P<month>{_MONTH_PATTERN})\.?\s+(?P<day>\d{{1,2}})"
    r"(?:st|nd|rd|th)?,?\s+(?P<year>\d{4})\b",
    re.IGNORECASE,
)
_DAY_MONTH_YEAR_RE = re.compile(
    rf"\b(?P<day>\d{{1,2}})(?:st|nd|rd|th)?\s+"
    rf"(?P<month>{_MONTH_PATTERN})\.?,?\s+(?P<year>\d{{4}})\b",
    re.IGNORECASE,
)
_MONTH_DAY_RE = re.compile(
    rf"\b(?P<month>{_MONTH_PATTERN})\.?\s+(?P<day>\d{{1,2}})"
    r"(?:st|nd|rd|th)?\b(?!\s*,?\s*\d{4}\b)",
    re.IGNORECASE,
)
_DAY_MONTH_RE = re.compile(
    rf"\b(?P<day>\d{{1,2}})(?:st|nd|rd|th)?\s+"
    rf"(?P<month>{_MONTH_PATTERN})\.?\b(?!\s*,?\s*\d{{4}}\b)",
    re.IGNORECASE,
)
_YMD_NUMERIC_RE = re.compile(
    r"(?<![\d/.])(?P<year>\d{4})(?P<sep>[/.])(?P<month>\d{1,2})"
    r"(?P=sep)(?P<day>\d{1,2})(?![\d/.])"
)
_NUMERIC_FULL_YEAR_RE = re.compile(
    r"(?<![\d/-])(?P<first>\d{1,2})(?P<sep>[/-])(?P<second>\d{1,2})"
    r"(?P=sep)(?P<year>\d{4})(?![\d/-])"
)
_NUMERIC_SHORT_YEAR_RE = re.compile(
    r"(?<![\d/-])\d{1,2}(?P<sep>[/-])\d{1,2}(?P=sep)\d{2}(?![\d/-])"
)
_NUMERIC_NO_YEAR_RE = re.compile(
    r"(?<![\d/-])(?P<first>\d{1,2})[/-](?P<second>\d{1,2})"
    r"(?![/-]\d)(?![\d/-])"
)
_SHARED_MONTH_RANGE_RE = re.compile(
    rf"\b(?P<month>{_MONTH_PATTERN})\.?\s+(?P<first>\d{{1,2}})"
    r"(?:st|nd|rd|th)?\s*(?P<connector>-|–|—|to|through|until)\s*"
    r"(?P<last>\d{1,2})(?:st|nd|rd|th)?"
    r"(?:,?\s+(?P<year>\d{4})\b)?(?!\s*,?\s*\d+\b)",
    re.IGNORECASE,
)
_SHARED_DAY_FIRST_RANGE_RE = re.compile(
    r"\b(?P<first>\d{1,2})(?:st|nd|rd|th)?\s*"
    r"(?P<connector>-|–|—|to|through|until)\s*"
    rf"(?P<last>\d{{1,2}})(?:st|nd|rd|th)?\s+"
    rf"(?P<month>{_MONTH_PATTERN})\.?,?"
    r"(?:\s+(?P<year>\d{4})\b)?(?!\s*,?\s*\d+\b)",
    re.IGNORECASE,
)
# These source-grammar patterns intentionally mirror the strict reschedule
# shapes in gmail_temporal_review.  They are repeated here instead of importing
# the review layer (which depends on this module), and are narrowed to horizontal
# whitespace so abbreviated alternatives can never cross a clause boundary.
_ABBREVIATED_RESCHEDULE_DAY_TAIL_RE = re.compile(
    r"[ \t]*(?:(?:,[ \t]*)?\bor[ \t]+|/[ \t]*)"
    r"(?P<token>(?P<day>\d{1,2})(?:st|nd|rd|th)?)"
    r"(?![\w/:])"
    r"(?![ \t]*,?[ \t]*\d{4}\b)"
    r"(?![ \t]*(?:a\.?m\.?|p\.?m\.?)\b)",
    re.IGNORECASE,
)
_ABBREVIATED_RESCHEDULE_FROM_PREFIX_RE = re.compile(
    r"\b(?:changed|moved|postponed|rescheduled|pushed[ \t]+back)\b"
    r"(?:[ \t]+(?:again|once[ \t]+more))?[ \t]+from[ \t]*$",
    re.IGNORECASE,
)
_ABBREVIATED_RESCHEDULE_REPLACEMENT_PREFIX_RE = re.compile(
    r"(?:"
    r"\b(?:moved|postponed|rescheduled|pushed[ \t]+back)\b"
    r"(?:[ \t]+(?:again|once[ \t]+more))?[ \t]+(?:for|to|until)|"
    r"\bnew[ \t]+(?:date|time)[ \t]*:"
    r")[ \t]*$",
    re.IGNORECASE,
)
_ABBREVIATED_RESCHEDULE_CUE_PREFIX_RE = re.compile(
    r"\b(?:moved|postponed|rescheduled|pushed[ \t]+back)\b"
    r"[ \t]*:?[ \t]*$",
    re.IGNORECASE,
)
_ABBREVIATED_RESCHEDULE_NOW_PREFIX_RE = re.compile(r"\bnow[ \t]*$", re.IGNORECASE)
_ABBREVIATED_RESCHEDULE_DIRECT_CONNECTOR_RE = re.compile(
    r"[ \t]+(?:to|until)[ \t]+", re.IGNORECASE
)
_ABBREVIATED_RESCHEDULE_FROM_CONNECTOR_RE = re.compile(
    r"[ \t]+from[ \t]+", re.IGNORECASE
)
_ABBREVIATED_RESCHEDULE_INSTEAD_CONNECTOR_RE = re.compile(
    r"[ \t]+(?:instead[ \t]+of|rather[ \t]+than)[ \t]+", re.IGNORECASE
)
_ABBREVIATED_RESCHEDULE_WAS_CONNECTOR_RE = re.compile(
    r"[ \t]*\([ \t]*(?:previously|was)[ \t]+", re.IGNORECASE
)
_ABBREVIATED_RESCHEDULE_ARROW_RE = re.compile(
    r"[ \t]*(?:<->|->|=>|<-|<=|↔|⇄|→|←)[ \t]*"
)
_ABBREVIATED_RESCHEDULE_CLOSING_TAIL_RE = re.compile(r"[ \t]*(?:[)\]}\"'”’][ \t]*)*")
_HARD_CLAUSE_BOUNDARY_RE = re.compile(r"[.!?;\r\n]")
_RANGE_CONNECTOR_RE = re.compile(
    rf"^\s*(?:-|–|—|\bto\b|\bthrough\b|\buntil\b)\s*"
    rf"(?:(?:{_WEEKDAY_PATTERN})\s*,?\s*)?$",
    re.IGNORECASE,
)
_RELATIVE_RE = re.compile(
    rf"\b(?:today|tomorrow|yesterday|tonight|"
    rf"(?:(?:this\s+coming|this|next|last)\s+)?(?:{_WEEKDAY_PATTERN})|"
    r"in\s+\d{1,3}\s+(?:hours?|days?|weeks?))\b",
    re.IGNORECASE,
)
_COARSE_RELATIVE_RE = re.compile(
    r"\b(?:(?:today|tomorrow|this)\s+"
    r"(?:morning|afternoon|evening|noon|midday|midnight|lunch|"
    r"end\s+of\s+day|close\s+of\s+business)|"
    r"that\s+(?:morning|afternoon|evening|night)|"
    r"(?:morning|afternoon|evening|noon|midday|midnight|lunch|"
    r"end\s+of\s+day|close\s+of\s+business)\s+(?:today|tomorrow)|"
    r"(?:this|next|last)\s+(?:week|month|quarter|weekend)|"
    r"within\s+(?:\d{1,3}|a|one|two|three|few)\s+(?:business\s+)?days?|"
    r"in\s+(?:a\s+)?few\s+days?)\b",
    re.IGNORECASE,
)
_RECURRENCE_RE = re.compile(
    rf"\b(?:every\s+(?:day|weekday|week|month|quarter|year|{_WEEKDAY_PATTERN})|"
    r"daily|weekly|biweekly|monthly|quarterly|yearly|annually)\b",
    re.IGNORECASE,
)

_ZONE_PATTERN = (
    r"(?:Z|UTC|GMT|(?:UTC|GMT)\s*[+-]\d{1,2}(?::?\d{2})?|"
    r"[+-]\d{2}:?\d{2}|PST|PDT|MST|MDT|CST|CDT|EST|EDT|"
    r"(?:Africa|America|Antarctica|Arctic|Asia|Atlantic|Australia|Europe|"
    r"Indian|Pacific)/[A-Za-z_+-]+(?:/[A-Za-z_+-]+)?)"
)
_CLOCK_RE = re.compile(
    rf"(?<![\d:])(?P<hour>[01]?\d|2[0-3])"
    rf"(?::(?P<minute>[0-5]\d)(?::(?P<second>[0-5]\d)"
    rf"(?:\.(?P<fraction>\d{{1,6}}))?)?)?\s*"
    rf"(?P<ampm>a\.?m\.?|p\.?m\.?)?\s*(?P<zone>{_ZONE_PATTERN})?"
    r"(?![\w:])",
    re.IGNORECASE,
)
_CLOCK_LINK_RE = re.compile(r"^\s*(?:(?:,?\s*(?:at|@)\s+)|T|,\s*|\s+)$", re.IGNORECASE)

_EVENT_NOUN_RE = re.compile(
    r"\b(?:appointment|booking|briefing|call|ceremony|class|conference|concert|"
    r"consultation|demo|delivery|dinner|event|exam|flight|forum|hearing|"
    r"interview|kickoff|launch|meeting|offsite|orientation|party|pickup|"
    r"presentation|rehearsal|reservation|review|screening|seminar|session|stay|"
    r"summit|tour|training|trip|visit|webinar|workshop)\b",
    re.IGNORECASE,
)
_DEADLINE_MENTION_RE = re.compile(
    r"\b(?:deadline|due\s+date|expires?|expiry|no\s+later\s+than|rsvp)\b"
    r"|\bdue\b(?!\s+to\b)",
    re.IGNORECASE,
)
_ACTION_MENTION_RE = re.compile(
    rf"\b(?:{'|'.join(GMAIL_TEMPORAL_ACTION_VERBS)})\b",
    re.IGNORECASE,
)
_EVENT_PREDICATE_RE = re.compile(
    r"\b(?:attend|became\s+effective|becomes?\s+effective|catch\s+up|"
    r"check\s+in|closed|closes|connect|host|is\s+effective|join|meet|opened|"
    r"opens|present|speak|sync|takes?\s+effect|talk|took\s+effect|"
    r"went\s+into\s+effect|"
    r"will\s+(?:be\s+effective|become\s+effective|close|open|take\s+effect))\b",
    re.IGNORECASE,
)
_EFFECTIVE_STATE_PREDICATE_RE = re.compile(
    r"(?:became\s+effective|becomes?\s+effective|is\s+effective|"
    r"takes?\s+effect|took\s+effect|went\s+into\s+effect|"
    r"will\s+(?:be\s+effective|become\s+effective|take\s+effect))\Z",
    re.IGNORECASE,
)
_EFFECTIVE_STATE_OBSERVATION_RE = re.compile(r"is\s+effective\Z", re.IGNORECASE)
_OPENING_PREDICATE_RE = re.compile(
    r"(?:open|opened|opens|will\s+open)\Z",
    re.IGNORECASE,
)
_ACTUAL_OPENING_PREDICATE_RE = re.compile(r"opened\Z", re.IGNORECASE)
_CLOSING_PREDICATE_RE = re.compile(
    r"(?:close|closed|closes|will\s+close)\Z",
    re.IGNORECASE,
)
_ACTUAL_CLOSING_PREDICATE_RE = re.compile(r"closed\Z", re.IGNORECASE)
_OPENING_PREDICATE_SCAN_RE = re.compile(
    r"\b(?:open|opened|opens|will\s+open)\b", re.IGNORECASE
)
_BARE_INTAKE_OPENING_PREDICATE_RE = re.compile(r"\bopen\b", re.IGNORECASE)
_BARE_INTAKE_CLOSING_PREDICATE_RE = re.compile(r"\bclose\b", re.IGNORECASE)
_INTAKE_BOUNDARY_PREDICATE_RE = re.compile(
    r"(?P<state>\b(?:(?:is|are|will[ \t]+be|remains?)[ \t]+open))"
    r"(?:[ \t]+from\b)|"
    r"(?P<transition>\b(?:open|begins?|starts?)\b)",
    re.IGNORECASE,
)
_EFFECTIVE_BOUNDARY_PREDICATE_RES = (
    re.compile(
        r"\b(?P<predicate>effective[ \t]+date[ \t]+"
        r"(?:is|will[ \t]+be))\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?P<predicate>(?:is|was|will[ \t]+be)[ \t]+in[ \t]+force[ \t]+"
        r"(?:beginning|starting))\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?P<predicate>appl(?:y|ies)[ \t]+as[ \t]+of)\b",
        re.IGNORECASE,
    ),
)
_EFFECTIVE_BOUNDARY_SUBJECT_RE = re.compile(
    # Allow one domain modifier (benefit/benefits or coverage) before a
    # plan/policy head. Arbitrary adjectives and prepositional tails do not
    # gain authority merely because they contain an accepted noun later.
    r"\A(?:(?:benefits?|coverage)[ \t]+(?:plan|policy|policies)|"
    r"agreement|benefits?|contract|coverage|law|plan|policy|policies|"
    r"program|regulation|rule|terms?)(?:['\N{RIGHT SINGLE QUOTATION MARK}]s)?"
    r"(?:[ \t]+(?:change|changes|update))?[ \t]*\Z",
    re.IGNORECASE,
)
_PREDICATE_EXPRESSION_LINK_RE = re.compile(
    r"[ \t]*(?:[:,@-][ \t]*)?"
    r"(?:(?:at|by|from|no[ \t]+later[ \t]+than|on)[ \t]+)?\Z",
    re.IGNORECASE,
)
_PREDICATE_ADDITIONAL_EXPRESSION_LINK_RE = re.compile(
    r"[ \t]*(?:for[ \t]+[A-Za-z0-9]"
    r"[A-Za-z0-9 /'\N{RIGHT SINGLE QUOTATION MARK}-]{0,40})?"
    r"[ \t]*(?:,?[ \t]*(?:and|or)[ \t]+)\Z",
    re.IGNORECASE,
)
_INTAKE_POST_EXPRESSION_BOUNDARY_RE = re.compile(r"[ \t]*(?:[.!?;)\]}\"'”’][ \t]*)*")
_INTAKE_TRAILING_INDEPENDENT_CLAUSE_RE = re.compile(
    r"[ \t]*(?:,[ \t]*)?(?:and|but|then|while)[ \t]+\S",
    re.IGNORECASE,
)
_OPENING_RESOURCE_SUBJECT_RE = re.compile(
    r"(?:online[ \t]+)?portal(?:[ \t]+(?:window|period|cycle|intake))?",
    re.IGNORECASE,
)
_INTAKE_NAME_FIRST_PATTERN = (
    r"(?-i:[A-Z][A-Za-z0-9]*(?:[-'\N{RIGHT SINGLE QUOTATION MARK}]"
    r"[A-Za-z0-9]+)?)"
)
_INTAKE_NAME_TOKEN_PATTERN = rf"(?:{_INTAKE_NAME_FIRST_PATTERN}|(?-i:[0-9]{{2,8}}))"
_INTAKE_NAME_LEADING_BLOCKER_PATTERN = (
    r"i|we|you|they|he|she|please|keep|use|review|reviewed|maintain|leave|"
    r"hold|consider|mark|make|set|test|tested|see|saw|show|report|reported"
)
_INTAKE_NAME_PREFIX_PATTERN = (
    rf"(?!(?i:{_INTAKE_NAME_LEADING_BLOCKER_PATTERN})\b)"
    rf"{_INTAKE_NAME_FIRST_PATTERN}"
    rf"(?:[ \t]+{_INTAKE_NAME_TOKEN_PATTERN}){{1,3}}[ \t]+"
)
_INTAKE_NAME_TARGET_PATTERN = (
    rf"{_INTAKE_NAME_FIRST_PATTERN}"
    rf"(?:[ \t]+{_INTAKE_NAME_TOKEN_PATTERN}){{1,4}}"
)
_INTAKE_APPLICATION_WINDOW_PATTERN = (
    r"application[ \t]+(?:cycle|intake|period|portal|window)"
)
_INTAKE_SINGULAR_SUBJECT_PATTERN = (
    rf"(?:{_INTAKE_NAME_PREFIX_PATTERN}"
    rf"(?:registration|{_INTAKE_APPLICATION_WINDOW_PATTERN})|"
    rf"registration(?:[ \t]+for[ \t]+{_INTAKE_NAME_TARGET_PATTERN})?|"
    rf"{_INTAKE_APPLICATION_WINDOW_PATTERN}"
    rf"(?:[ \t]+for[ \t]+{_INTAKE_NAME_TARGET_PATTERN})?)"
)
_INTAKE_PLURAL_SUBJECT_PATTERN = (
    rf"(?:{_INTAKE_NAME_PREFIX_PATTERN}applications|"
    rf"applications(?:[ \t]+for[ \t]+{_INTAKE_NAME_TARGET_PATTERN})?)"
)
_THIRD_PERSON_INTAKE_OPENING_SUBJECT_RES = {
    "open": re.compile(
        rf"\A[ \t]*(?:(?:our|the|your)[ \t]+)?"
        rf"(?P<subject>{_INTAKE_PLURAL_SUBJECT_PATTERN})[ \t]*\Z",
        re.IGNORECASE,
    ),
    "opens": re.compile(
        rf"\A[ \t]*(?:(?:our|the|your)[ \t]+)?"
        rf"(?P<subject>{_INTAKE_SINGULAR_SUBJECT_PATTERN})[ \t]*\Z",
        re.IGNORECASE,
    ),
}
_OPENING_TO_CLOSING_COORDINATION_RE = re.compile(
    r"[ \t]*(?:,[ \t]*)?(?:and|but|then)[ \t]+",
    re.IGNORECASE,
)
_TRAILING_COORDINATION_RE = re.compile(r"(?:,\s*)?(?:and|but|then)\s*\Z", re.IGNORECASE)
_CLOSURE_SUBJECT_SEPARATOR_RE = re.compile(
    r"(?:[,;:\r\n]+|\b(?:and|but|then)\b)\s*", re.IGNORECASE
)
_DEADLINE_CLOSURE_SUBJECT_RE = re.compile(
    r"\b(?:"
    r"applications(?:\s+(?:window|period|portal|cycle|intake))?|"
    r"application\s+(?:window|period|portal|cycle|intake)|"
    r"entries(?:\s+(?:window|period|portal|cycle|intake))?|"
    r"entry\s+(?:window|period|portal|cycle|intake)|"
    r"enrollment(?:\s+(?:window|period|portal|cycle|intake))?|"
    r"nominations(?:\s+(?:window|period|portal|cycle|intake))?|"
    r"nomination\s+(?:window|period|portal|cycle|intake)|"
    r"registration(?:\s+(?:window|period|portal|cycle|intake))?|"
    r"rsvp(?:\s+(?:window|period|portal|cycle|intake))?|"
    r"sign[ -]?ups?(?:\s+(?:window|period|portal|cycle|intake))?|"
    r"submissions(?:\s+(?:window|period|portal|cycle|intake))?|"
    r"submission\s+(?:window|period|portal|cycle|intake)"
    r")\b"
    r"(?:\s+(?:for|to)\s+(?:(?!\b(?:and|but|then)\b)[^,;.!?\r\n]){1,80})?"
    r"\s*\Z",
    re.IGNORECASE,
)
_EVENT_TITLE_LABEL_RE = re.compile(
    r"(?im)^[ \t]*(?:event|title|what|summary)\s*:[ \t]*"
    r"(?P<title>[^\r\n]{3,160})"
)
# Some authored updates carry the durable event identity in the body rather than
# in the message subject.  Keep this rescue deliberately narrower than a noun-
# phrase parser: a capitalized name plus a recognized event head must begin a
# clause and participate in one of a few explicit temporal grammars.  Horizontal
# whitespace in every token prevents a title from crossing a source line or
# sentence boundary.
_SOURCE_BOUND_EVENT_HEAD_PATTERN = (
    r"appointment|booking|briefing|call|ceremony|class|conference|concert|"
    r"consultation|debrief|demo|dinner|event|exam|flight|forum|hearing|interview|"
    r"kickoff|launch|meeting|offsite|orientation|party|presentation|rehearsal|"
    r"reservation|review|screening|seminar|session|stay|summit|tour|training|"
    r"trip|visit|webinar|workshop"
)
_SOURCE_BOUND_EVENT_TOKEN_PATTERN = (
    r"(?:[A-Za-z][A-Za-z0-9]*"
    r"(?:[-'\N{RIGHT SINGLE QUOTATION MARK}][A-Za-z0-9]+)*|[0-9]{2,8})"
)
_SOURCE_BOUND_EVENT_NAME_PATTERN = (
    r"(?:[A-Z]|[a-z](?=[A-Za-z0-9]*[A-Z]))"
    r"[A-Za-z0-9]*(?:[-'\N{RIGHT SINGLE QUOTATION MARK}][A-Za-z0-9]+)*"
)
_SOURCE_BOUND_EVENT_DISCOURSE_PREFIX_PATTERN = (
    r"Separately|Independently|"
    r"In[ \t]+a[ \t]+separate[ \t]+item|"
    r"On[ \t]+another[ \t]+line"
)
_SOURCE_BOUND_EVENT_TITLE_RE = re.compile(
    rf"(?:\A|(?<=[.!?;\r\n]))[ \t]*"
    rf"(?:(?i:{_SOURCE_BOUND_EVENT_DISCOURSE_PREFIX_PATTERN})[ \t]*,[ \t]*)?"
    rf"(?i:The)[ \t]+"
    rf"(?P<title>{_SOURCE_BOUND_EVENT_NAME_PATTERN}"
    rf"(?:[ \t]+{_SOURCE_BOUND_EVENT_TOKEN_PATTERN}){{0,5}}[ \t]+"
    rf"(?i:{_SOURCE_BOUND_EVENT_HEAD_PATTERN}))"
    rf"[ \t]+(?P<predicate>"
    rf"(?i:may[ \t]+(?:happen|occur|take[ \t]+place)[ \t]+on)|"
    rf"(?i:has[ \t]+been[ \t]+rescheduled[ \t]+(?:to|from))|"
    rf"(?i:(?:(?:is|was|has[ \t]+been)[ \t]+)?scheduled[ \t]+(?:for|on))|"
    rf"(?i:(?:starts?|begins?|will[ \t]+(?:start|begin))"
    rf"(?:[ \t]+(?:at|on))?)"
    rf")\b"
)
_SOURCE_BOUND_EVENT_ROUTINE_MODIFIER_RE = re.compile(
    r"\b(?:account|billing|invoice|order|package|parcel|receipt|shipment|"
    r"subscription|tracking)\b",
    re.IGNORECASE,
)
_SOURCE_BOUND_EVENT_INTERNAL_CLAUSE_RE = re.compile(
    r"\b(?:i|we|you|he|she|they|it|"
    r"announce(?:d|s)?|confirm(?:ed|s)?|expect(?:ed|s)?|note(?:d|s)?|"
    r"report(?:ed|s)?|say|says|said|share(?:d|s)?|update(?:d|s)?)\b",
    re.IGNORECASE,
)
_SOURCE_BOUND_EVENT_ARTICLES = frozenset({"a", "an", "the"})
_SOURCE_BOUND_EVENT_ARTICLE_PREPOSITIONS = frozenset({"of"})
_SOURCE_BOUND_EVENT_MODIFIERS = frozenset(
    {
        "annual",
        "architecture",
        "birthday",
        "board",
        "budget",
        "client",
        "customer",
        "design",
        "executive",
        "final",
        "finance",
        "kickoff",
        "leadership",
        "monthly",
        "partner",
        "performance",
        "planning",
        "portfolio",
        "product",
        "project",
        "quarterly",
        "roadmap",
        "sales",
        "strategy",
        "team",
        "technical",
        "weekly",
        "yearly",
    }
)
_SOURCE_BOUND_FIRST_EXPRESSION_LINK_RE = re.compile(r"[ \t]*(?:(?i:either)[ \t]+)?")
_SOURCE_BOUND_RESCHEDULE_NEXT_LINK_RE = re.compile(
    r"[ \t]*(?:,[ \t]*)?(?P<role>from|to)[ \t]*",
    re.IGNORECASE,
)
_SOURCE_BOUND_OPTION_NEXT_LINK_RE = re.compile(
    r"[ \t]*(?:,[ \t]*)?(?:and|or)(?:[ \t]+(?:at|on))?[ \t]*",
    re.IGNORECASE,
)
GMAIL_TEMPORAL_CLAUSE_BOUND_EVENT_TITLE_BLOCKER = "clause_bound_event_title_review_only"
GMAIL_TEMPORAL_SUPERSEDED_HISTORICAL_BLOCKER = (
    "historical_tail_superseded_by_authored_update"
)
_SUBJECT_REPLY_PREFIX_RE = re.compile(r"(?i)^(?:(?:re|fw|fwd)\s*:\s*)+")
_GENERIC_EVENT_TITLE_RE = re.compile(
    r"(?i)^(?:calendar\s+)?(?:invite|invitation|reminder|notification|update|"
    r"newsletter|digest|receipt|order|reservation|confirmation|confirmed|"
    r"schedule|scheduled|event|meeting|appointment|webinar|promotion|sale|"
    r"offer|welcome|thank\s+you|no\s+subject)$"
)
_ARTIFACT_RE = re.compile(
    r"\b(?:agenda|attachment|brief|deck|details|dial-in|invite|invitation|"
    r"link|notes|preparation|prep|recording|reminder|room|summary|transcript)\b",
    re.IGNORECASE,
)
_FOOTER_MARKER_RE = re.compile(
    r"\b(?:manage\s+(?:email\s+)?preferences|privacy\s+policy|unsubscribe|"
    r"view\s+(?:this\s+email\s+)?in\s+(?:a\s+)?browser)\b|©",
    re.IGNORECASE,
)
_QUOTED_LINE_RE = re.compile(r"(?m)^[ \t]*>[^\n]*(?:\n|\Z)")
_FORWARDED_ORIGINAL_MARKER_RE = re.compile(
    r"(?im)^[ \t]*(?:"
    r"-{2,}[ \t]*(?:original|forwarded)[ \t]+message[ \t]*-{2,}|"
    r"-{2,}[ \t]*forwarded[ \t]+history[ \t]*-{2,}|"
    r"(?:>[ \t]*)+(?:\*\*|__)?[ \t]*archived[ \t]+note[ \t]*:?[ \t]*(?:\*\*|__)?|"
    r"begin[ \t]+forwarded[ \t]+message:?|"
    r"on[ \t]+[^\r\n]{1,500}[ \t]+wrote:"
    r")[ \t]*\r?$"
)
_NO_ACTIVE_DATE_CLAUSE_RE = re.compile(
    r"(?:there\s+is\s+)?no\s+(?:currently\s+)?active\s+date"
    r"(?:\s+for\s+(?:this|the)\s+(?:item|event|meeting|appointment))?",
    re.IGNORECASE,
)
_CURRENT_STATUS_SUBJECT_IDENTITY_RE = re.compile(
    r"(?:current\s+)?status(?:\s+update)?\s+(?:of|for)\s+(?P<identity>.+)",
    re.IGNORECASE,
)
_HISTORICAL_SCHEDULE_CLAUSE_RE = re.compile(
    r"\s*(?:are|had\s+been|has\s+been|is|previously|was|were|will\s+be)\s+"
    r"(?:re)?scheduled\b",
    re.IGNORECASE,
)
_HISTORICAL_CONJUNCT_BOUNDARY_RE = re.compile(
    r"[,;]\s*(?:and|but|then|whereas|while)\b",
    re.IGNORECASE,
)
_GENERIC_STATUS_IDENTITY_TOKENS = frozenset(
    {"appointment", "event", "item", "meeting", "status", "the", "this", "update"}
)
_BOUNDARY_RE = re.compile(
    r"\b(?:arrival|arrives?|check[ -]?out|completion|ends?|returns?)\b",
    re.IGNORECASE,
)
_LIFECYCLE_PATTERNS = (
    (
        "rescheduled",
        re.compile(r"\b(?:rescheduled|moved|new\s+time)\b", re.IGNORECASE),
    ),
    (
        "cancelled",
        re.compile(r"\b(?:cancelled|canceled|called\s+off)\b", re.IGNORECASE),
    ),
    (
        "completed",
        re.compile(
            r"\b(?:completed|concluded|ended|finished)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "scheduled",
        re.compile(r"\b(?:booked|confirmed|scheduled)\b", re.IGNORECASE),
    ),
)
_STRUCTURAL_LABEL_PATTERNS = (
    (
        "structural_label",
        "occurrence",
        "planned",
        "occurrence_start",
        (),
        re.compile(
            r"(?im)^[ \t]*(?:[-*\u2022]\s*)?(?:appointment\s+date|"
            r"departure|event\s+date|scheduled\s+for|starts?|start\s+time|when)\s*:",
        ),
    ),
    (
        "structural_label",
        None,
        None,
        None,
        (),
        re.compile(r"(?im)^[ \t]*(?:[-*\u2022]\s*)?(?:date|time)\s*:"),
    ),
    (
        "deadline",
        "deadline",
        "planned",
        "deadline",
        (),
        re.compile(
            r"(?im)^[ \t]*(?:[-*\u2022]\s*)?(?:complete\s+by|deadline|"
            r"due(?:\s+date)?|expires?|respond\s+by|rsvp\s+by|submit\s+by)\s*:",
        ),
    ),
    (
        "boundary",
        None,
        None,
        "terminal_boundary",
        ("terminal_boundary_not_occurrence_start",),
        re.compile(
            r"(?im)^[ \t]*(?:[-*\u2022]\s*)?(?:arrival|ends?|end\s+time)\s*:",
        ),
    ),
)
_PLANNED_CONTEXT_RE = re.compile(
    r"\b(?:booked|confirmed|planned|rescheduled|scheduled|targeted|upcoming)\b"
    r"|\bwill\s+(?:attend|be\s+effective|become\s+effective|begin|close|"
    r"connect|depart|happen|host|join|meet|occur|open|present|speak|start|"
    r"sync|take\s+effect|take\s+place|talk)\b",
    re.IGNORECASE,
)
_ACTUAL_CONTEXT_RE = re.compile(
    r"\b(?:became\s+effective|began|closed|departed|happened|occurred|opened|"
    r"started|took\s+effect|took\s+place|was\s+held|went\s+into\s+effect)\b",
    re.IGNORECASE,
)
_DEADLINE_ASSOCIATION_RE = re.compile(
    r"\b(?:by|before|deadline|due|no\s+later\s+than|until)\b", re.IGNORECASE
)
_DIRECT_ACTION_DEADLINE_CUE_RE = re.compile(
    r"(?:by|no\s+later\s+than)\Z", re.IGNORECASE
)
_COORDINATING_CLAUSE_BOUNDARY_RE = re.compile(
    r"(?:[,;]\s*(?:and|but|then)\b|\b(?:but|then)\b|"
    r"\b(?:is|are|was|were|has|have|had|will)\b)",
    re.IGNORECASE,
)
_FORBIDDEN_ASSOCIATION_PUNCTUATION_RE = re.compile(r"[.!?;]")

_NORTH_AMERICAN_ABBREVIATION_OFFSETS = {
    "PST": -8,
    "PDT": -7,
    "MST": -7,
    "MDT": -6,
    "CST": -6,
    "CDT": -5,
    "EST": -5,
    "EDT": -4,
}
_MAX_YEAR_INFERENCE_DISTANCE = 370
_MAX_LOCAL_ASSOCIATION_GAP = 60
_MAX_NEAR_ASSOCIATION_GAP = 240
_MAX_RETAINED_LOCAL_EDGES = 20
_MAX_RETAINED_NEAR_EDGES = 20
_MAX_RETAINED_BRIDGE_EDGES = 12
_MAX_BROAD_ASSOCIATION_EXPRESSIONS = 64
_MAX_BROAD_ASSOCIATION_MENTIONS = 128
_STRICT_ASSOCIATION_MENTION_TYPES = frozenset(
    {"event", "deadline", "action", "boundary"}
)
_CORE_ASSOCIATION_MENTION_TYPES = frozenset(
    {
        "event",
        "event_title_candidate",
        "deadline",
        "action",
        "boundary",
    }
)
_T = TypeVar("_T")


def analyze_gmail_temporal_leads(
    *,
    text: str,
    message_internal_at: str | datetime | None,
    fact_admitted: bool,
    temporal_review_rescue: bool = False,
    chunk_id: str | None = None,
) -> TemporalLeadAnalysis:
    """Inventory temporal evidence and optionally create review-only leads.

    This function performs no writes, routing, extraction, or persistence.
    ``fact_admitted`` and the explicit temporal-rescue gate affect association
    hints only; neither can manufacture or remove expression/mention evidence.
    Rescue output remains distinguishable so validation can force deferral.
    """

    source = text if isinstance(text, str) else ""
    scope_bound = isinstance(chunk_id, str) and bool(chunk_id.strip())
    anchor = _aware_internal_time(message_internal_at)
    fields = _field_ranges(source)
    segments = _segment_ranges(source, fields)
    quoted_or_forwarded_ranges = _quoted_or_forwarded_ranges(source)
    superseded_historical_ranges = _superseded_historical_ranges(source)
    scope_prefix = _opaque_scope_prefix(chunk_id, source)
    expressions = _expressions(
        source,
        anchor,
        fields,
        segments,
        quoted_or_forwarded_ranges,
        superseded_historical_ranges,
        scope_prefix,
    )
    mentions = _mentions(
        source,
        fields,
        segments,
        expressions,
        quoted_or_forwarded_ranges,
        superseded_historical_ranges,
        scope_prefix,
    )
    admission_basis: AssociationAdmissionBasis = (
        "fact"
        if fact_admitted is True
        else "temporal_rescue"
        if temporal_review_rescue is True
        else "none"
    )
    if admission_basis != "none":
        (
            leads,
            candidate_edge_count,
            graph_truncated,
            omitted_expression_count,
            omitted_mention_count,
        ) = _associate(
            source,
            expressions,
            mentions,
            anchor,
            chunk_id,
            scope_prefix,
        )
    else:
        leads = ()
        candidate_edge_count = 0
        graph_truncated = False
        omitted_expression_count = 0
        omitted_mention_count = 0
    version = "gmail_temporal_leads_v2"
    source_sha256 = hashlib.sha256(source.encode("utf-8")).hexdigest()
    snapshot_fingerprint = _analysis_snapshot_fingerprint(
        version=version,
        source_sha256=source_sha256,
        anchor=anchor,
        chunk_id=chunk_id,
        scope_bound=scope_bound,
        fact_admitted=fact_admitted is True,
        association_admission_basis=admission_basis,
        expressions=expressions,
        mentions=mentions,
        leads=leads,
        candidate_edge_count=candidate_edge_count,
        candidate_edge_count_exact=(
            omitted_expression_count == 0 and omitted_mention_count == 0
        ),
        omitted_expression_count=omitted_expression_count,
        omitted_mention_count=omitted_mention_count,
        graph_truncated=graph_truncated,
    )
    result = TemporalLeadAnalysis(
        version=version,
        snapshot_fingerprint=snapshot_fingerprint,
        source_sha256=source_sha256,
        scope_bound=scope_bound,
        fact_admitted=fact_admitted is True,
        association_admission_basis=admission_basis,
        expressions=expressions,
        mentions=mentions,
        leads=leads,
        candidate_edge_count=candidate_edge_count,
        candidate_edge_count_exact=(
            omitted_expression_count == 0 and omitted_mention_count == 0
        ),
        omitted_expression_count=omitted_expression_count,
        omitted_mention_count=omitted_mention_count,
        retained_edge_count=len(leads),
        graph_truncated=graph_truncated,
    )
    validate_gmail_temporal_lead_analysis_authority(
        result,
        expected_snapshot_fingerprint=snapshot_fingerprint,
        source_sha256=source_sha256,
        message_internal_at=message_internal_at,
        chunk_id=chunk_id,
    )
    return result


def validate_gmail_temporal_lead_analysis_authority(
    analysis: TemporalLeadAnalysis,
    *,
    expected_snapshot_fingerprint: str,
    source_sha256: str,
    message_internal_at: str | datetime | None,
    chunk_id: str | None,
) -> None:
    """Rebind one content-free analysis to its exact trusted Gmail inputs.

    The snapshot fingerprint is an integrity receipt, not merely an identifier.
    Recomputing it here prevents a frozen analysis from being copied with altered
    mentions, expressions, leads, or counters while retaining the old receipt.
    Callers must independently obtain ``expected_snapshot_fingerprint`` and the
    other inputs from the current trusted review head.
    """

    scope_bound = isinstance(chunk_id, str) and bool(chunk_id.strip())
    count_values = (
        (
            analysis.candidate_edge_count,
            analysis.omitted_expression_count,
            analysis.omitted_mention_count,
            analysis.retained_edge_count,
        )
        if isinstance(analysis, TemporalLeadAnalysis)
        else ()
    )
    if (
        not isinstance(analysis, TemporalLeadAnalysis)
        or analysis.version != "gmail_temporal_leads_v2"
        or not isinstance(expected_snapshot_fingerprint, str)
        or not expected_snapshot_fingerprint.startswith("gta_")
        or len(expected_snapshot_fingerprint) != 68
        or not isinstance(source_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", source_sha256) is None
        or analysis.source_sha256 != source_sha256
        or analysis.scope_bound is not scope_bound
        or not isinstance(analysis.fact_admitted, bool)
        or analysis.association_admission_basis
        not in {
            "none",
            "fact",
            "temporal_rescue",
        }
        or (analysis.association_admission_basis == "fact")
        is not analysis.fact_admitted
        or not isinstance(analysis.expressions, tuple)
        or not isinstance(analysis.mentions, tuple)
        or not isinstance(analysis.leads, tuple)
        or any(
            not isinstance(item, TemporalExpression) for item in analysis.expressions
        )
        or any(not isinstance(item, TemporalMention) for item in analysis.mentions)
        or any(not isinstance(item, TemporalLead) for item in analysis.leads)
        or any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in count_values
        )
        or analysis.retained_edge_count != len(analysis.leads)
        or analysis.candidate_edge_count < analysis.retained_edge_count
        or analysis.candidate_edge_count_exact
        is not (
            analysis.omitted_expression_count == 0
            and analysis.omitted_mention_count == 0
        )
        or not isinstance(analysis.graph_truncated, bool)
    ):
        raise ValueError("temporal analysis authority is invalid or stale")

    expression_ids = tuple(item.expression_id for item in analysis.expressions)
    mention_ids = tuple(item.mention_id for item in analysis.mentions)
    lead_ids = tuple(item.lead_id for item in analysis.leads)
    if (
        len(expression_ids) != len(set(expression_ids))
        or len(mention_ids) != len(set(mention_ids))
        or len(lead_ids) != len(set(lead_ids))
        or any(
            lead.expression_id not in set(expression_ids)
            or lead.mention_id not in set(mention_ids)
            for lead in analysis.leads
        )
    ):
        raise ValueError("temporal analysis authority graph is invalid")

    expected = _analysis_snapshot_fingerprint(
        version=analysis.version,
        source_sha256=source_sha256,
        anchor=_aware_internal_time(message_internal_at),
        chunk_id=chunk_id,
        scope_bound=scope_bound,
        fact_admitted=analysis.fact_admitted,
        association_admission_basis=analysis.association_admission_basis,
        expressions=analysis.expressions,
        mentions=analysis.mentions,
        leads=analysis.leads,
        candidate_edge_count=analysis.candidate_edge_count,
        candidate_edge_count_exact=analysis.candidate_edge_count_exact,
        omitted_expression_count=analysis.omitted_expression_count,
        omitted_mention_count=analysis.omitted_mention_count,
        graph_truncated=analysis.graph_truncated,
    )
    if (
        analysis.snapshot_fingerprint != expected
        or expected_snapshot_fingerprint != expected
    ):
        raise ValueError("temporal analysis fingerprint is invalid or stale")


def _expressions(
    text: str,
    anchor: datetime | None,
    fields: tuple[_FieldRange, ...],
    segments: tuple[_SegmentRange, ...],
    quoted_or_forwarded_ranges: tuple[tuple[int, int], ...],
    superseded_historical_ranges: tuple[tuple[int, int], ...],
    scope_prefix: str,
) -> tuple[TemporalExpression, ...]:
    anchor_day = anchor.date() if anchor else None
    ranges = _shared_range_drafts(text, anchor_day)
    atoms = _date_drafts(text, anchor_day)
    abbreviated_reschedule_days = _abbreviated_reschedule_day_drafts(
        text,
        atoms=atoms,
        segments=segments,
    )
    ranges.extend(_paired_range_drafts(text, atoms, ranges))
    occupied = [(item.start, item.end) for item in ranges]
    drafts: list[_ExpressionDraft] = list(ranges)
    for atom in atoms:
        if _overlaps(atom.start, atom.end, occupied):
            continue
        attached = _attach_clock(text, atom)
        drafts.append(attached)
        occupied.append((attached.start, attached.end))
    for abbreviated in abbreviated_reschedule_days:
        if not _overlaps(abbreviated.start, abbreviated.end, occupied):
            # Preserve the exact abbreviated-day span.  In particular, do not
            # let the ordinary clock attachment pass absorb adjacent source.
            drafts.append(abbreviated)
            occupied.append((abbreviated.start, abbreviated.end))
    for unresolved in _unresolved_temporal_drafts(text, anchor):
        if not _overlaps(unresolved.start, unresolved.end, occupied):
            drafts.append(unresolved)
            occupied.append((unresolved.start, unresolved.end))
    for relative in _relative_drafts(text, anchor):
        attached = _attach_clock(text, relative)
        if not _overlaps(attached.start, attached.end, occupied):
            drafts.append(attached)
            occupied.append((attached.start, attached.end))
    for clock in _time_only_drafts(text):
        if not _overlaps(clock.start, clock.end, occupied):
            drafts.append(clock)
            occupied.append((clock.start, clock.end))

    unique: list[_ExpressionDraft] = []
    seen: set[tuple[object, ...]] = set()
    for item in sorted(
        drafts, key=lambda value: (value.start, -(value.end - value.start))
    ):
        key = (
            item.start,
            item.end,
            item.form,
            item.normalized_options,
            item.resolution_basis,
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return tuple(
        TemporalExpression(
            expression_id=f"{scope_prefix}:e{index}",
            start=item.start,
            end=item.end,
            field=_field_for_span(fields, item.start, item.end),
            segment_id=_segment_id_for_span(segments, item.start, item.end),
            form=item.form,
            normalized_options=item.normalized_options,
            calendar_date_options=(
                item.calendar_date_options
                or _calendar_date_options(item.normalized_options)
            ),
            local_time=item.local_time,
            precision=item.precision,
            resolution_status=_resolution_status(item.normalized_options),
            resolution_basis=item.resolution_basis,
            blockers=_ordered_unique(
                (
                    *item.blockers,
                    *_quoted_or_forwarded_blocker(
                        item.start,
                        item.end,
                        quoted_or_forwarded_ranges,
                    ),
                    *_superseded_historical_blocker(
                        item.start,
                        item.end,
                        superseded_historical_ranges,
                    ),
                    *(
                        ("multiple_normalization_options",)
                        if len(item.normalized_options) > 1
                        else ()
                    ),
                    *(
                        ("multiple_calendar_date_options",)
                        if len(
                            item.calendar_date_options
                            or _calendar_date_options(item.normalized_options)
                        )
                        > 1
                        else ()
                    ),
                )
            ),
        )
        for index, item in enumerate(unique, start=1)
    )


def _date_drafts(text: str, anchor: date | None) -> list[_ExpressionDraft]:
    drafts: list[_ExpressionDraft] = []
    for pattern, form, basis in (
        (_ISO_DATE_RE, "explicit_date", "explicit_iso_date"),
        (_MONTH_DAY_YEAR_RE, "explicit_date", "explicit_month_day_year"),
        (_DAY_MONTH_YEAR_RE, "explicit_date", "explicit_day_month_year"),
    ):
        for match in pattern.finditer(text):
            value = _date_from_match(match)
            drafts.append(
                _ExpressionDraft(
                    match.start(),
                    match.end(),
                    form,
                    (value.isoformat(),) if value else (),
                    "day",
                    (basis,),
                    () if value else ("invalid_calendar_date",),
                )
            )
    for pattern, basis in (
        (_MONTH_DAY_RE, "month_day_year_inferred_from_message_internal_at"),
        (_DAY_MONTH_RE, "day_month_year_inferred_from_message_internal_at"),
    ):
        for match in pattern.finditer(text):
            options = _inferred_date_options(
                _month_number(match.group("month")), int(match.group("day")), anchor
            )
            drafts.append(
                _ExpressionDraft(
                    match.start(),
                    match.end(),
                    "inferred_date",
                    tuple(value.isoformat() for value in options),
                    "day",
                    (basis,),
                    (
                        ("inferred_year_from_message_time",)
                        if options
                        else (
                            ("missing_year_anchor",)
                            if anchor is None
                            else ("invalid_calendar_date",)
                        )
                    ),
                )
            )
    for match in _YMD_NUMERIC_RE.finditer(text):
        value = _safe_date(
            int(match.group("year")),
            int(match.group("month")),
            int(match.group("day")),
        )
        drafts.append(
            _ExpressionDraft(
                match.start(),
                match.end(),
                "numeric_date",
                (value.isoformat(),) if value else (),
                "day",
                ("numeric_year_month_day",),
                () if value else ("invalid_numeric_date",),
            )
        )
    for match in _NUMERIC_FULL_YEAR_RE.finditer(text):
        values, basis = _numeric_order_dates(
            int(match.group("first")),
            int(match.group("second")),
            int(match.group("year")),
        )
        drafts.append(
            _ExpressionDraft(
                match.start(),
                match.end(),
                "numeric_date",
                tuple(value.isoformat() for value in values),
                "day",
                (basis,),
                () if values else ("invalid_numeric_date",),
            )
        )
    for match in _NUMERIC_SHORT_YEAR_RE.finditer(text):
        drafts.append(
            _ExpressionDraft(
                match.start(),
                match.end(),
                "numeric_date",
                (),
                "day",
                ("numeric_short_year_not_expanded",),
                ("ambiguous_short_year",),
            )
        )
    for match in _NUMERIC_NO_YEAR_RE.finditer(text):
        values, basis = _numeric_order_dates_with_inferred_year(
            int(match.group("first")), int(match.group("second")), anchor
        )
        drafts.append(
            _ExpressionDraft(
                match.start(),
                match.end(),
                "numeric_date",
                tuple(value.isoformat() for value in values),
                "day",
                (basis,),
                (
                    ("inferred_year_from_message_time",)
                    if values
                    else (
                        ("missing_year_anchor",)
                        if anchor is None
                        else ("invalid_numeric_date",)
                    )
                ),
            )
        )

    output: list[_ExpressionDraft] = []
    occupied: list[tuple[int, int]] = []
    for item in sorted(
        drafts, key=lambda value: (value.start, -(value.end - value.start))
    ):
        if _overlaps(item.start, item.end, occupied):
            continue
        output.append(item)
        occupied.append((item.start, item.end))
    return output


def _abbreviated_reschedule_day_drafts(
    text: str,
    *,
    atoms: list[_ExpressionDraft],
    segments: tuple[_SegmentRange, ...],
) -> list[_ExpressionDraft]:
    """Inventory a shared-month day only inside strict reschedule grammar.

    A source such as ``August 15, 2027 or 16`` contains a real second endpoint,
    but the generic date parser must not treat arbitrary bare numbers as dates.
    This bounded pass starts only from a fully qualified textual date, requires
    an immediately adjacent ``or`` or slash tail in the same clause, and checks
    that the date occupies an explicit reschedule endpoint slot.  The inherited
    date is still blocked for review because source alternatives are unresolved.
    """

    output: list[_ExpressionDraft] = []
    ordered_atoms = sorted(atoms, key=lambda item: (item.start, item.end))
    for anchor in ordered_atoms:
        anchor_day = _explicit_textual_anchor_day(anchor)
        if anchor_day is None:
            continue
        segment = next(
            (
                item
                for item in segments
                if item.start <= anchor.start and anchor.end <= item.end
            ),
            None,
        )
        if segment is None:
            continue
        match = _ABBREVIATED_RESCHEDULE_DAY_TAIL_RE.match(
            text,
            anchor.end,
            segment.end,
        )
        if match is None:
            continue
        token_start, token_end = match.span("token")
        clause_start, clause_end = _hard_clause_bounds(
            text,
            span_start=anchor.start,
            span_end=anchor.end,
            lower_bound=segment.start,
            upper_bound=segment.end,
            protected_spans=tuple((item.start, item.end) for item in ordered_atoms),
        )
        if token_end > clause_end:
            continue
        local_atoms = tuple(
            item
            for item in ordered_atoms
            if clause_start <= item.start and item.end <= clause_end
        )
        if not _abbreviated_reschedule_tail_is_bounded(
            text,
            anchor=anchor,
            shorthand_end=token_end,
            clause_end=clause_end,
            atoms=local_atoms,
        ):
            continue
        if not _is_governed_reschedule_endpoint(
            text,
            anchor=anchor,
            shorthand_end=token_end,
            clause_start=clause_start,
            atoms=local_atoms,
        ):
            continue

        value = _safe_date(
            anchor_day.year,
            anchor_day.month,
            int(match.group("day")),
        )
        options = (value.isoformat(),) if value is not None else ()
        output.append(
            _ExpressionDraft(
                token_start,
                token_end,
                "abbreviated_shared_month_day",
                options,
                "day",
                (
                    "month_and_year_inherited_from_preceding_explicit_"
                    "reschedule_endpoint",
                ),
                _ordered_unique(
                    (
                        "reschedule_endpoint_alternatives_unresolved",
                        *(("invalid_calendar_date",) if value is None else ()),
                    )
                ),
                options,
            )
        )
    return output


def _explicit_textual_anchor_day(item: _ExpressionDraft) -> date | None:
    if (
        item.form != "explicit_date"
        or len(item.normalized_options) != 1
        or not {
            "explicit_month_day_year",
            "explicit_day_month_year",
        }.intersection(item.resolution_basis)
    ):
        return None
    try:
        return date.fromisoformat(item.normalized_options[0])
    except ValueError:
        return None


def _hard_clause_bounds(
    text: str,
    *,
    span_start: int,
    span_end: int,
    lower_bound: int,
    upper_bound: int,
    protected_spans: tuple[tuple[int, int], ...],
) -> tuple[int, int]:
    boundaries = tuple(
        match.start()
        for match in _HARD_CLAUSE_BOUNDARY_RE.finditer(
            text,
            lower_bound,
            upper_bound,
        )
        if not any(
            protected_start <= match.start() < protected_end
            for protected_start, protected_end in protected_spans
        )
    )
    preceding = max((index for index in boundaries if index < span_start), default=-1)
    clause_start = max(lower_bound, preceding + 1)
    following = tuple(index for index in boundaries if index >= span_end)
    clause_end = min(following, default=upper_bound)
    return clause_start, clause_end


def _is_governed_reschedule_endpoint(
    text: str,
    *,
    anchor: _ExpressionDraft,
    shorthand_end: int,
    clause_start: int,
    atoms: tuple[_ExpressionDraft, ...],
) -> bool:
    """Match only endpoint slots accepted by the strict review grammar."""

    prefix = text[clause_start : anchor.start]
    if (
        _ABBREVIATED_RESCHEDULE_FROM_PREFIX_RE.search(prefix) is not None
        or _ABBREVIATED_RESCHEDULE_REPLACEMENT_PREFIX_RE.search(prefix) is not None
    ):
        return True

    try:
        anchor_index = next(index for index, item in enumerate(atoms) if item is anchor)
    except StopIteration:
        return False

    if anchor_index > 0:
        previous = atoms[anchor_index - 1]
        previous_prefix = text[clause_start : previous.start]
        connector = text[previous.end : anchor.start]
        if (
            _ABBREVIATED_RESCHEDULE_FROM_PREFIX_RE.search(previous_prefix) is not None
            and _ABBREVIATED_RESCHEDULE_DIRECT_CONNECTOR_RE.fullmatch(connector)
            is not None
        ):
            return True
        if (
            _ABBREVIATED_RESCHEDULE_CUE_PREFIX_RE.search(previous_prefix) is not None
            and _ABBREVIATED_RESCHEDULE_ARROW_RE.fullmatch(connector) is not None
        ):
            return True

    if anchor_index + 1 < len(atoms):
        following = atoms[anchor_index + 1]
        if shorthand_end <= following.start:
            connector = text[shorthand_end : following.start]
            if (
                _ABBREVIATED_RESCHEDULE_CUE_PREFIX_RE.search(prefix) is not None
                and _ABBREVIATED_RESCHEDULE_ARROW_RE.fullmatch(connector) is not None
            ):
                return True
            if (
                _ABBREVIATED_RESCHEDULE_NOW_PREFIX_RE.search(prefix) is not None
                and _ABBREVIATED_RESCHEDULE_INSTEAD_CONNECTOR_RE.fullmatch(connector)
                is not None
            ):
                return True
    return False


def _abbreviated_reschedule_tail_is_bounded(
    text: str,
    *,
    anchor: _ExpressionDraft,
    shorthand_end: int,
    clause_end: int,
    atoms: tuple[_ExpressionDraft, ...],
) -> bool:
    """Reject a bare count followed by prose while retaining endpoint grammar."""

    following = next(
        (item for item in atoms if item is not anchor and shorthand_end <= item.start),
        None,
    )
    if following is not None:
        connector = text[shorthand_end : following.start]
        return any(
            pattern.fullmatch(connector) is not None
            for pattern in (
                _ABBREVIATED_RESCHEDULE_DIRECT_CONNECTOR_RE,
                _ABBREVIATED_RESCHEDULE_FROM_CONNECTOR_RE,
                _ABBREVIATED_RESCHEDULE_INSTEAD_CONNECTOR_RE,
                _ABBREVIATED_RESCHEDULE_WAS_CONNECTOR_RE,
                _ABBREVIATED_RESCHEDULE_ARROW_RE,
            )
        )
    if clause_end < len(text) and text[clause_end] in "\r\n":
        return False
    return (
        _ABBREVIATED_RESCHEDULE_CLOSING_TAIL_RE.fullmatch(
            text[shorthand_end:clause_end]
        )
        is not None
    )


def _shared_range_drafts(text: str, anchor: date | None) -> list[_ExpressionDraft]:
    output: list[_ExpressionDraft] = []
    occupied: list[tuple[int, int]] = []
    for pattern in (_SHARED_MONTH_RANGE_RE, _SHARED_DAY_FIRST_RANGE_RE):
        for match in pattern.finditer(text):
            if _overlaps(match.start(), match.end(), occupied):
                continue
            month = _month_number(match.group("month"))
            explicit_year = match.group("year")
            inferred = (
                ()
                if explicit_year
                else _inferred_date_options(month, int(match.group("first")), anchor)
            )
            years = (
                (int(explicit_year),)
                if explicit_year
                else tuple(dict.fromkeys(value.year for value in inferred))
            )
            options_list: list[str] = []
            for year in years:
                first = _safe_date(year, month, int(match.group("first")))
                last = _safe_date(year, month, int(match.group("last")))
                options_list.extend(
                    _interval_options(first, last, match.group("connector"))
                )
            options = tuple(dict.fromkeys(options_list))
            invalid_range = not options and (bool(explicit_year) or anchor is not None)
            output.append(
                _ExpressionDraft(
                    match.start(),
                    match.end(),
                    "date_range",
                    options,
                    "day",
                    (
                        "textual_range_with_explicit_year"
                        if explicit_year
                        else "textual_range_year_inferred_from_message_internal_at",
                        _range_basis(match.group("connector")),
                    ),
                    _ordered_unique(
                        (
                            *(
                                ()
                                if explicit_year
                                else (
                                    ("inferred_year_from_message_time",)
                                    if inferred
                                    else (
                                        ("missing_year_anchor",)
                                        if anchor is None
                                        else ("invalid_calendar_date",)
                                    )
                                )
                            ),
                            *(
                                ("invalid_or_nonascending_range",)
                                if invalid_range
                                else ()
                            ),
                        )
                    ),
                )
            )
            occupied.append((match.start(), match.end()))
    return output


def _paired_range_drafts(
    text: str,
    atoms: list[_ExpressionDraft],
    existing: list[_ExpressionDraft],
) -> list[_ExpressionDraft]:
    occupied = [(item.start, item.end) for item in existing]
    output: list[_ExpressionDraft] = []
    for first, last in zip(atoms, atoms[1:]):
        if _overlaps(first.start, last.end, occupied):
            continue
        gap = text[first.end : last.start]
        if not _RANGE_CONNECTOR_RE.fullmatch(gap):
            continue
        if _is_lifecycle_reassignment(text, first.start, gap):
            # "rescheduled from X to Y" describes two schedule assertions, not
            # one occurrence interval. Leave both endpoint expressions intact.
            continue
        options: list[str] = []
        for first_value in first.normalized_options:
            for last_value in last.normalized_options:
                try:
                    start_day = date.fromisoformat(first_value)
                    end_day = date.fromisoformat(last_value)
                except ValueError:
                    continue
                options.extend(_interval_options(start_day, end_day, gap.strip()))
        blockers = _ordered_unique(
            (
                *first.blockers,
                *last.blockers,
                *(("invalid_or_nonascending_range",) if not options else ()),
            )
        )
        output.append(
            _ExpressionDraft(
                first.start,
                last.end,
                "date_range",
                tuple(dict.fromkeys(options)),
                "day",
                (*first.resolution_basis, *last.resolution_basis, _range_basis(gap)),
                blockers,
            )
        )
        occupied.append((first.start, last.end))
    return output


def _relative_drafts(text: str, anchor: datetime | None) -> list[_ExpressionDraft]:
    output: list[_ExpressionDraft] = []
    for match in _RELATIVE_RE.finditer(text):
        raw = re.sub(r"\s+", " ", match.group(0).strip().casefold())
        options: tuple[str, ...] = ()
        precision = "day"
        basis = "relative_expression"
        blockers: tuple[str, ...] = ("relative_to_message_time",)
        if anchor is None:
            blockers = ("missing_relative_anchor",)
        elif raw == "today":
            options = (anchor.date().isoformat(),)
            basis = "relative_today_from_message_internal_at"
        elif raw == "tomorrow":
            options = ((anchor.date() + timedelta(days=1)).isoformat(),)
            basis = "relative_tomorrow_from_message_internal_at"
        elif raw == "yesterday":
            options = ((anchor.date() - timedelta(days=1)).isoformat(),)
            basis = "relative_yesterday_from_message_internal_at"
        elif raw == "tonight":
            options = (anchor.date().isoformat(),)
            basis = "relative_tonight_from_message_internal_at"
            blockers = (*blockers, "time_of_day_unresolved")
        elif raw.startswith("in "):
            count, unit = re.fullmatch(
                r"in\s+(\d{1,3})\s+(hours?|days?|weeks?)", raw
            ).groups()
            amount = int(count)
            if unit.startswith("hour"):
                options = ((anchor + timedelta(hours=amount)).isoformat(),)
                precision = "exact"
            else:
                days = amount * (7 if unit.startswith("week") else 1)
                options = ((anchor.date() + timedelta(days=days)).isoformat(),)
            basis = f"relative_offset_{unit.rstrip('s')}_from_message_internal_at"
        else:
            qualifier_match = re.fullmatch(
                rf"(?:(this\s+coming|this|next|last)\s+)?({_WEEKDAY_PATTERN})",
                raw,
            )
            if qualifier_match and anchor:
                qualifier = qualifier_match.group(1) or "bare"
                weekday = _WEEKDAYS[qualifier_match.group(2)]
                values = _weekday_options(anchor.date(), weekday, qualifier)
                options = tuple(value.isoformat() for value in values)
                basis = f"relative_{qualifier.replace(' ', '_')}_weekday_from_message_internal_at"
                if qualifier in {"bare", "next"}:
                    blockers = (*blockers, "ambiguous_weekday_convention")
        output.append(
            _ExpressionDraft(
                match.start(),
                match.end(),
                "relative_date",
                tuple(dict.fromkeys(options)),
                precision,
                (basis,),
                _ordered_unique(blockers),
            )
        )
    return output


def _unresolved_temporal_drafts(
    text: str,
    anchor: datetime | None,
) -> list[_ExpressionDraft]:
    output: list[_ExpressionDraft] = []
    for match in _COARSE_RELATIVE_RE.finditer(text):
        raw = re.sub(r"\s+", " ", match.group(0).strip().casefold())
        if raw.startswith("that "):
            output.append(
                _ExpressionDraft(
                    match.start(),
                    match.end(),
                    "coarse_relative",
                    (),
                    "coarse",
                    (
                        "anaphoric_coarse_relative_expression",
                        "coarse_time_of_day_expression",
                    ),
                    (
                        "anaphoric_reference_unresolved",
                        "time_of_day_unresolved",
                        "coarse_relative_unresolved",
                    ),
                )
            )
            continue
        anchored_day = next(
            (value for value in ("today", "tomorrow") if value in raw.split()),
            None,
        )
        has_coarse_time_of_day = any(
            value in raw
            for value in (
                "morning",
                "afternoon",
                "evening",
                "noon",
                "midday",
                "midnight",
                "lunch",
                "end of day",
                "close of business",
            )
        )
        if has_coarse_time_of_day and (
            anchored_day is not None or raw.startswith("this ")
        ):
            options: tuple[str, ...] = ()
            blockers: tuple[str, ...]
            basis = "coarse_relative_expression"
            if anchor is None:
                blockers = ("missing_relative_anchor", "time_of_day_unresolved")
            else:
                offset = 1 if anchored_day == "tomorrow" else 0
                options = ((anchor.date() + timedelta(days=offset)).isoformat(),)
                basis = (
                    "relative_tomorrow_from_message_internal_at"
                    if offset
                    else "relative_today_from_message_internal_at"
                )
                blockers = (
                    "relative_to_message_time",
                    "time_of_day_unresolved",
                )
            output.append(
                _ExpressionDraft(
                    match.start(),
                    match.end(),
                    "coarse_relative",
                    options,
                    "day",
                    (basis, "coarse_time_of_day_expression"),
                    blockers,
                )
            )
            continue
        output.append(
            _ExpressionDraft(
                match.start(),
                match.end(),
                "coarse_relative",
                (),
                "coarse",
                ("coarse_relative_expression",),
                ("coarse_relative_unresolved",),
            )
        )
    for match in _RECURRENCE_RE.finditer(text):
        output.append(
            _ExpressionDraft(
                match.start(),
                match.end(),
                "recurrence",
                (),
                "recurrence",
                ("recurrence_expression",),
                ("recurrence_not_expanded",),
            )
        )
    return output


def _attach_clock(text: str, atom: _ExpressionDraft) -> _ExpressionDraft:
    tail = text[atom.end : atom.end + 100]
    clock = _CLOCK_RE.search(tail)
    if clock is None or not _CLOCK_LINK_RE.fullmatch(tail[: clock.start()]):
        return atom
    if not _clock_has_time(clock):
        return atom
    end = atom.end + clock.end()
    calendar_options = atom.calendar_date_options or _calendar_date_options(
        atom.normalized_options
    )
    exact, basis, blockers, local_time = _clock_options(clock, calendar_options)
    # Once the expression asserts a wall time, a date-only value is not a complete
    # normalization of that expression. Keep date and wall-clock components typed,
    # but expose no normalized option until an instant is available.
    options = exact
    precision = "exact" if exact else "local_time"
    inherited_blockers = tuple(
        value for value in atom.blockers if value != "time_of_day_unresolved"
    )
    return _ExpressionDraft(
        atom.start,
        end,
        "date_time",
        options,
        precision,
        (*atom.resolution_basis, *basis),
        _ordered_unique((*inherited_blockers, *blockers)),
        calendar_options,
        local_time,
    )


def _time_only_drafts(text: str) -> list[_ExpressionDraft]:
    output: list[_ExpressionDraft] = []
    for match in _CLOCK_RE.finditer(text):
        if not _clock_has_time(match) or _clock_is_likely_non_temporal(text, match):
            continue
        _options, basis, blockers, local_time = _clock_options(match, ())
        output.append(
            _ExpressionDraft(
                match.start(),
                match.end(),
                "time_only",
                (),
                "time",
                ("time_only_without_date", *basis),
                _ordered_unique(("missing_calendar_date", *blockers)),
                (),
                local_time,
            )
        )
    return output


def _clock_options(
    match: re.Match[str], date_options: tuple[str, ...]
) -> tuple[
    tuple[str, ...],
    tuple[str, ...],
    tuple[str, ...],
    TemporalLocalTime,
]:
    hour = int(match.group("hour"))
    minute = int(match.group("minute") or 0)
    second = int(match.group("second") or 0)
    fraction = match.group("fraction") or ""
    microsecond = int(fraction.ljust(6, "0")) if fraction else 0
    ampm = match.group("ampm")
    hours: tuple[int, ...]
    blockers: list[str] = []
    if ampm:
        if not 1 <= hour <= 12:
            local_time = TemporalLocalTime(
                (), minute, second, microsecond, None, None, None
            )
            return (), (), ("invalid_clock_time",), local_time
        marker = ampm.replace(".", "").casefold()
        hours = (hour % 12 + (12 if marker == "pm" else 0),)
    elif hour <= 12 and len(match.group("hour")) == 1:
        hours = tuple(dict.fromkeys((hour, (hour + 12) % 24)))
        blockers.append("ambiguous_meridiem")
    else:
        hours = (hour,)

    zone_text = match.group("zone")
    if not zone_text:
        local_time = TemporalLocalTime(
            hours, minute, second, microsecond, None, None, None
        )
        return (
            (),
            ("clock_time_without_timezone",),
            tuple((*blockers, "missing_timezone")),
            local_time,
        )
    tzinfo, zone_basis, zone_blocker = _timezone_for_text(zone_text)
    if tzinfo is None:
        local_time = TemporalLocalTime(
            hours,
            minute,
            second,
            microsecond,
            "invalid_timezone",
            None,
            zone_text.strip(),
        )
        return (
            (),
            ("clock_time_with_invalid_timezone",),
            tuple((*blockers, "invalid_timezone")),
            local_time,
        )
    if zone_blocker:
        blockers.append(zone_blocker)
    offset = tzinfo.utcoffset(None) if not isinstance(tzinfo, ZoneInfo) else None
    local_time = TemporalLocalTime(
        hours,
        minute,
        second,
        microsecond,
        zone_basis,
        int(offset.total_seconds() // 60) if offset is not None else None,
        zone_text.strip(),
    )

    output: list[str] = []
    invalid_local = False
    for day_value in date_options:
        try:
            day = date.fromisoformat(day_value)
        except ValueError:
            continue
        for resolved_hour in hours:
            if isinstance(tzinfo, ZoneInfo) and not _iana_local_time_is_unambiguous(
                day, resolved_hour, minute, second, microsecond, tzinfo
            ):
                invalid_local = True
                continue
            value = datetime.combine(
                day,
                time(resolved_hour, minute, second, microsecond),
                tzinfo=tzinfo,
            )
            output.append(
                value.isoformat(timespec="microseconds" if microsecond else "seconds")
            )
    if invalid_local:
        blockers.append("invalid_or_ambiguous_local_time")
    return (
        tuple(dict.fromkeys(output)),
        ("explicit_clock_time", zone_basis),
        tuple(blockers),
        local_time,
    )


def _mentions(
    text: str,
    fields: tuple[_FieldRange, ...],
    segments: tuple[_SegmentRange, ...],
    expressions: tuple[TemporalExpression, ...],
    quoted_or_forwarded_ranges: tuple[tuple[int, int], ...],
    superseded_historical_ranges: tuple[tuple[int, int], ...],
    scope_prefix: str,
) -> tuple[TemporalMention, ...]:
    drafts: list[_MentionDraft] = []
    for match in _EVENT_NOUN_RE.finditer(text):
        if gmail_temporal_event_head_is_artifact_object(
            text,
            event_head_start=match.start(),
        ):
            continue
        field = _field_for_span(fields, match.start(), match.end())
        kind = _context_kind(text, fields, field, match.start(), match.end())
        drafts.append(
            _MentionDraft(
                match.start(),
                match.end(),
                "event",
                "occurrence",
                kind,
                "occurrence_start" if kind else None,
                None,
            )
        )
    for match in _EVENT_PREDICATE_RE.finditer(text):
        field = _field_for_span(fields, match.start(), match.end())
        surface = match.group(0)
        opening_predicate = _OPENING_PREDICATE_RE.fullmatch(surface) is not None
        if opening_predicate and not _opening_predicate_context_supported(
            text,
            fields=fields,
            segments=segments,
            expressions=expressions,
            predicate_start=match.start(),
            predicate_end=match.end(),
        ):
            continue
        predicate_blockers = ["predicate_mention_review_only"]
        if opening_predicate:
            relation: TemporalRelation = "occurrence"
            kind: TemporalKind | None = (
                "actual"
                if _ACTUAL_OPENING_PREDICATE_RE.fullmatch(surface)
                else "planned"
            )
            boundary_role = "occurrence_start"
        elif _CLOSING_PREDICATE_RE.fullmatch(surface):
            closing_kind: TemporalKind = (
                "actual"
                if _ACTUAL_CLOSING_PREDICATE_RE.fullmatch(surface)
                else "planned"
            )
            if _deadline_closure_context(
                text,
                fields,
                field,
                match.start(),
                match.end(),
            ):
                relation = "deadline"
                kind = closing_kind
                boundary_role = "deadline"
            else:
                relation = "occurrence"
                kind = (
                    closing_kind
                    if _ACTUAL_CLOSING_PREDICATE_RE.fullmatch(surface)
                    else _context_kind(
                        text,
                        fields,
                        field,
                        match.start(),
                        match.end(),
                    )
                )
                boundary_role = "terminal_boundary"
                predicate_blockers.append("terminal_boundary_not_occurrence_start")
        elif _EFFECTIVE_STATE_PREDICATE_RE.fullmatch(surface):
            relation = "occurrence"
            kind = _context_kind(text, fields, field, match.start(), match.end())
            if _EFFECTIVE_STATE_OBSERVATION_RE.fullmatch(surface):
                boundary_role = None
                predicate_blockers.append("effective_state_not_transition")
            else:
                boundary_role = "occurrence_start"
        else:
            relation = "occurrence"
            kind = _context_kind(text, fields, field, match.start(), match.end())
            boundary_role = "occurrence_start" if kind else None
        drafts.append(
            _MentionDraft(
                match.start(),
                match.end(),
                "event_predicate",
                relation,
                kind,
                boundary_role,
                None,
                tuple(predicate_blockers),
            )
        )
        if opening_predicate and surface.casefold() == "opens":
            subject_span = _third_person_intake_opening_subject_span(
                text,
                fields=fields,
                field_name=field,
                predicate_start=match.start(),
            )
            if subject_span is not None:
                drafts.append(
                    _MentionDraft(
                        subject_span[0],
                        subject_span[1],
                        "event",
                        "occurrence",
                        "planned",
                        "occurrence_start",
                        None,
                    )
                )
    for match in _BARE_INTAKE_OPENING_PREDICATE_RE.finditer(text):
        field = _field_for_span(fields, match.start(), match.end())
        subject_span = _third_person_intake_opening_subject_span(
            text,
            fields=fields,
            field_name=field,
            predicate_start=match.start(),
        )
        if subject_span is None or not _opening_predicate_context_supported(
            text,
            fields=fields,
            segments=segments,
            expressions=expressions,
            predicate_start=match.start(),
            predicate_end=match.end(),
        ):
            continue
        drafts.append(
            _MentionDraft(
                match.start(),
                match.end(),
                "event_predicate",
                "occurrence",
                "planned",
                "occurrence_start",
                None,
                ("predicate_mention_review_only",),
            )
        )
        drafts.append(
            _MentionDraft(
                subject_span[0],
                subject_span[1],
                "event",
                "occurrence",
                "planned",
                "occurrence_start",
                None,
            )
        )
    for match in _BARE_INTAKE_CLOSING_PREDICATE_RE.finditer(text):
        field = _field_for_span(fields, match.start(), match.end())
        if not _deadline_closure_context(
            text,
            fields,
            field,
            match.start(),
            match.end(),
        ) or not _predicate_has_forward_expression(
            text,
            fields=fields,
            segments=segments,
            expressions=expressions,
            predicate_start=match.start(),
            link_end=match.end(),
        ):
            continue
        drafts.append(
            _MentionDraft(
                match.start(),
                match.end(),
                "event_predicate",
                "deadline",
                "planned",
                "deadline",
                None,
                ("predicate_mention_review_only",),
            )
        )
    drafts.extend(
        _bounded_event_predicate_drafts(
            text,
            fields=fields,
            segments=segments,
            expressions=expressions,
        )
    )
    for match in _DEADLINE_MENTION_RE.finditer(text):
        drafts.append(
            _MentionDraft(
                match.start(),
                match.end(),
                "deadline",
                "deadline",
                "planned",
                "deadline",
                None,
            )
        )
    for match in _ACTION_MENTION_RE.finditer(text):
        drafts.append(
            _MentionDraft(
                match.start(),
                match.end(),
                "action",
                None,
                None,
                None,
                None,
            )
        )
    for match in _ARTIFACT_RE.finditer(text):
        drafts.append(
            _MentionDraft(
                match.start(),
                match.end(),
                "artifact",
                None,
                None,
                None,
                None,
                ("artifact_context",),
            )
        )
    for match in _FOOTER_MARKER_RE.finditer(text):
        drafts.append(
            _MentionDraft(
                match.start(),
                match.end(),
                "artifact",
                None,
                None,
                None,
                None,
                ("footer_marker_context",),
            )
        )
    for match in _BOUNDARY_RE.finditer(text):
        drafts.append(
            _MentionDraft(
                match.start(),
                match.end(),
                "boundary",
                "occurrence",
                None,
                "terminal_boundary",
                None,
                ("terminal_boundary_not_occurrence_start",),
            )
        )
    for (
        mention_type,
        relation,
        kind,
        boundary_role,
        blockers,
        pattern,
    ) in _STRUCTURAL_LABEL_PATTERNS:
        for match in pattern.finditer(text):
            if any(
                item.mention_type != "artifact"
                and item.start < match.end()
                and match.start() < item.end
                for item in drafts
            ):
                continue
            drafts.append(
                _MentionDraft(
                    match.start(),
                    match.end(),
                    mention_type,
                    relation,
                    kind,
                    boundary_role,
                    None,
                    blockers,
                )
            )
    for role, pattern in _LIFECYCLE_PATTERNS:
        for match in pattern.finditer(text):
            blocker = (
                (f"lifecycle_{role}",)
                if role in {"rescheduled", "cancelled", "completed"}
                else ()
            )
            drafts.append(
                _MentionDraft(
                    match.start(),
                    match.end(),
                    "lifecycle",
                    ("occurrence" if role in {"scheduled", "rescheduled"} else None),
                    "planned" if role in {"scheduled", "rescheduled"} else None,
                    {
                        "scheduled": "occurrence_start",
                        "rescheduled": "replacement_time",
                        "cancelled": "cancelled",
                        "completed": "terminal_boundary",
                    }[role],
                    role,
                    blocker,
                )
            )

    drafts.extend(
        _event_title_drafts(
            text,
            fields=fields,
            segments=segments,
            expressions=expressions,
            quoted_or_forwarded_ranges=quoted_or_forwarded_ranges,
        )
    )

    unique: list[_MentionDraft] = []
    seen: set[tuple[object, ...]] = set()
    for item in sorted(
        drafts,
        key=lambda value: (value.start, value.end, value.mention_type),
    ):
        key = (item.start, item.end, item.mention_type, item.lifecycle_role)
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return tuple(
        TemporalMention(
            mention_id=f"{scope_prefix}:m{index}",
            start=item.start,
            end=item.end,
            field=_field_for_span(fields, item.start, item.end),
            segment_id=_segment_id_for_span(segments, item.start, item.end),
            mention_type=item.mention_type,
            relation=item.relation,
            kind=item.kind,
            boundary_role=item.boundary_role,
            lifecycle_role=item.lifecycle_role,
            blockers=_ordered_unique(
                (
                    *item.blockers,
                    *_quoted_or_forwarded_blocker(
                        item.start,
                        item.end,
                        quoted_or_forwarded_ranges,
                    ),
                    *_superseded_historical_blocker(
                        item.start,
                        item.end,
                        superseded_historical_ranges,
                    ),
                )
            ),
        )
        for index, item in enumerate(unique, start=1)
    )


def _event_title_drafts(
    text: str,
    *,
    fields: tuple[_FieldRange, ...],
    segments: tuple[_SegmentRange, ...],
    expressions: tuple[TemporalExpression, ...],
    quoted_or_forwarded_ranges: tuple[tuple[int, int], ...],
) -> list[_MentionDraft]:
    """Return conservative proper-title endpoints for deferred review.

    Event titles expand the subject inventory without turning arbitrary noun
    phrases into trusted entities.  A subject title requires both a recognized
    event cue in the subject and temporal evidence in the body.  A labeled body
    title requires a recognized temporal expression in the same segment or
    within a bounded local window.
    """

    output: list[_MentionDraft] = []
    subject = next((item for item in fields if item.name == "subject"), None)
    body_expressions = tuple(item for item in expressions if item.field == "body")
    subject_has_event_cue = bool(
        subject is not None
        and (
            _EVENT_NOUN_RE.search(text, subject.start, subject.end)
            or _EVENT_PREDICATE_RE.search(text, subject.start, subject.end)
        )
    )
    if subject is not None and body_expressions and subject_has_event_cue:
        span = _trim_event_title_span(text, subject.start, subject.end)
        if (
            span is not None
            and not _span_overlaps_expressions(span, expressions)
            and _event_title_span_is_eligible(text, span)
        ):
            output.append(
                _MentionDraft(
                    span[0],
                    span[1],
                    "event_title_candidate",
                    "occurrence",
                    None,
                    None,
                    None,
                    ("event_title_review_only",),
                )
            )

    for match in _EVENT_TITLE_LABEL_RE.finditer(text):
        span = _trim_event_title_span(
            text,
            match.start("title"),
            match.end("title"),
        )
        if (
            span is None
            or _span_overlaps_expressions(span, expressions)
            or not _event_title_span_is_eligible(text, span)
        ):
            continue
        segment_id = _segment_id_for_span(segments, span[0], span[1])
        if not any(
            expression.segment_id == segment_id
            or _span_distance(span[0], span[1], expression.start, expression.end) <= 600
            for expression in expressions
        ):
            continue
        output.append(
            _MentionDraft(
                span[0],
                span[1],
                "event_title_candidate",
                "occurrence",
                None,
                None,
                None,
                ("event_title_review_only",),
            )
        )

    output.extend(
        _source_bound_clause_event_title_drafts(
            text,
            fields=fields,
            segments=segments,
            expressions=expressions,
            quoted_or_forwarded_ranges=quoted_or_forwarded_ranges,
        )
    )
    return output


def _source_bound_clause_event_title_drafts(
    text: str,
    *,
    fields: tuple[_FieldRange, ...],
    segments: tuple[_SegmentRange, ...],
    expressions: tuple[TemporalExpression, ...],
    quoted_or_forwarded_ranges: tuple[tuple[int, int], ...],
) -> list[_MentionDraft]:
    """Recover a full event identity from a small, source-bound grammar.

    These candidates are review-only and may associate only with expressions in
    their own source segment.  The immediate right-hand expression check avoids
    turning a merely topical event phrase into a temporal subject, while the
    provenance check prevents quoted or forwarded history from gaining a fresh
    event identity.
    """

    output: list[_MentionDraft] = []
    for match in _SOURCE_BOUND_EVENT_TITLE_RE.finditer(text):
        span = match.span("title")
        if any(
            range_start < match.end() and match.start() < range_end
            for range_start, range_end in quoted_or_forwarded_ranges
        ):
            continue
        if _SOURCE_BOUND_EVENT_ROUTINE_MODIFIER_RE.search(text[span[0] : span[1]]):
            continue
        if _source_bound_event_title_has_internal_clause(text[span[0] : span[1]]):
            continue
        if not _event_title_span_is_eligible(text, span):
            continue

        field = _field_for_span(fields, span[0], span[1])
        segment_id = _segment_id_for_span(segments, span[0], span[1])
        right_hand_expressions = tuple(
            expression
            for expression in expressions
            if expression.field == field
            and expression.segment_id == segment_id
            and expression.start >= match.end("predicate")
        )
        if not right_hand_expressions:
            continue
        first_expression = min(right_hand_expressions, key=lambda item: item.start)
        if (
            re.fullmatch(
                r"[ \t]*(?:(?i:either)[ \t]+)?",
                text[match.end("predicate") : first_expression.start],
            )
            is None
        ):
            continue

        output.append(
            _MentionDraft(
                span[0],
                span[1],
                "event_title_candidate",
                "occurrence",
                None,
                None,
                None,
                (
                    "event_title_review_only",
                    GMAIL_TEMPORAL_CLAUSE_BOUND_EVENT_TITLE_BLOCKER,
                ),
            )
        )
    return output


def _source_bound_event_title_has_internal_clause(value: str) -> bool:
    """Reject prose-shaped titles with a closed proper-name/title grammar."""

    if _SOURCE_BOUND_EVENT_INTERNAL_CLAUSE_RE.search(value):
        return True
    tokens = tuple(re.findall(r"[A-Za-z0-9]+", value))
    if len(tokens) < 2:
        return True
    for index, token in enumerate(tokens[1:-1], start=1):
        normalized = token.casefold()
        if normalized in _SOURCE_BOUND_EVENT_ARTICLES:
            if (
                tokens[index - 1].casefold()
                not in _SOURCE_BOUND_EVENT_ARTICLE_PREPOSITIONS
            ):
                return True
            continue
        if (
            normalized in _SOURCE_BOUND_EVENT_ARTICLE_PREPOSITIONS
            or normalized in _SOURCE_BOUND_EVENT_MODIFIERS
            or token[0].isupper()
            or any(character.isupper() for character in token[1:])
            or token[0].isdigit()
        ):
            continue
        return True
    return False


def _bounded_event_predicate_drafts(
    text: str,
    *,
    fields: tuple[_FieldRange, ...],
    segments: tuple[_SegmentRange, ...],
    expressions: tuple[TemporalExpression, ...],
) -> list[_MentionDraft]:
    """Return source-bound transition cues omitted by the broad vocabulary.

    Bare ``open``/``begin`` and state-like effective language are too generic
    to inventory globally. Admit them only when a recognized source subject is
    followed locally by an already parsed temporal expression. They remain
    predicate review hints, so the existing defer policy continues to apply.
    """

    output: list[_MentionDraft] = []
    for match in _INTAKE_BOUNDARY_PREDICATE_RE.finditer(text):
        group = "state" if match.group("state") is not None else "transition"
        start, end = match.span(group)
        field = _field_for_span(fields, start, end)
        if not _intake_boundary_subject_context(
            text,
            fields=fields,
            field_name=field,
            predicate_start=start,
        ):
            continue
        if not _predicate_has_forward_expression(
            text,
            fields=fields,
            segments=segments,
            expressions=expressions,
            predicate_start=start,
            link_end=match.end(),
            require_terminal_expression=True,
        ):
            continue
        output.append(
            _MentionDraft(
                start,
                end,
                "event_predicate",
                "occurrence",
                "planned",
                "occurrence_start",
                None,
                ("predicate_mention_review_only",),
            )
        )

    for pattern in _EFFECTIVE_BOUNDARY_PREDICATE_RES:
        for match in pattern.finditer(text):
            start, end = match.span("predicate")
            field = _field_for_span(fields, start, end)
            if not _effective_boundary_subject_context(
                text,
                fields=fields,
                field_name=field,
                predicate_start=start,
            ):
                continue
            if not _predicate_has_forward_expression(
                text,
                fields=fields,
                segments=segments,
                expressions=expressions,
                predicate_start=start,
                link_end=match.end(),
                require_terminal_expression=True,
            ):
                continue
            output.append(
                _MentionDraft(
                    start,
                    end,
                    "event_predicate",
                    "occurrence",
                    None,
                    "occurrence_start",
                    None,
                    ("predicate_mention_review_only",),
                )
            )
    return output


def _opening_predicate_context_supported(
    text: str,
    *,
    fields: tuple[_FieldRange, ...],
    segments: tuple[_SegmentRange, ...],
    expressions: tuple[TemporalExpression, ...],
    predicate_start: int,
    predicate_end: int,
) -> bool:
    """Bound inflected opening verbs to a subject and right-hand boundary."""

    field = _field_for_span(fields, predicate_start, predicate_end)
    if not _opening_boundary_subject_context(
        text,
        fields=fields,
        field_name=field,
        predicate_start=predicate_start,
    ):
        return False
    segment = next(
        (
            item
            for item in segments
            if item.start <= predicate_start and predicate_end <= item.end
        ),
        None,
    )
    if segment is None or segment.field != field:
        return False
    for expression in expressions:
        gap = expression.start - predicate_end
        if (
            expression.field == field
            and expression.segment_id == segment.segment_id
            and segment.start <= expression.start
            and expression.end <= segment.end
            and 0 <= gap <= 24
            and _PREDICATE_EXPRESSION_LINK_RE.fullmatch(
                text[predicate_end : expression.start]
            )
            is not None
            and _opening_expression_tail_is_supported(
                text,
                expression=expression,
                segment=segment,
                expressions=expressions,
            )
        ):
            return True
    return False


def _opening_boundary_subject_context(
    text: str,
    *,
    fields: tuple[_FieldRange, ...],
    field_name: TemporalField,
    predicate_start: int,
) -> bool:
    if (
        _third_person_intake_opening_subject_span(
            text,
            fields=fields,
            field_name=field_name,
            predicate_start=predicate_start,
        )
        is not None
    ):
        return True
    if _intake_boundary_subject_context(
        text,
        fields=fields,
        field_name=field_name,
        predicate_start=predicate_start,
    ):
        return True
    field = next((item for item in fields if item.name == field_name), None)
    lower, _upper = _clause_window(
        text,
        predicate_start,
        predicate_start,
        padding=100,
        lower_bound=field.start if field else 0,
        upper_bound=field.end if field else len(text),
    )
    subject = _CLOSURE_SUBJECT_SEPARATOR_RE.split(text[lower:predicate_start])[-1]
    subject = re.sub(
        r"\A(?:our|the|your)[ \t]+",
        "",
        subject.strip(),
        flags=re.IGNORECASE,
    )
    return _OPENING_RESOURCE_SUBJECT_RE.fullmatch(subject) is not None


def _third_person_intake_opening_subject_span(
    text: str,
    *,
    fields: tuple[_FieldRange, ...],
    field_name: TemporalField,
    predicate_start: int,
) -> tuple[int, int] | None:
    """Return the explicit intake subject of an agreeing ``open(s)`` frame."""

    field = next((item for item in fields if item.name == field_name), None)
    lower, _upper = _clause_window(
        text,
        predicate_start,
        predicate_start,
        padding=100,
        lower_bound=field.start if field else 0,
        upper_bound=field.end if field else len(text),
    )
    cue = re.match(r"(?:opens|open)\b", text[predicate_start:], re.IGNORECASE)
    if cue is None:
        return None
    pattern = _THIRD_PERSON_INTAKE_OPENING_SUBJECT_RES[cue.group(0).casefold()]
    match = pattern.fullmatch(text[lower:predicate_start])
    if match is None:
        return None
    start, end = match.span("subject")
    return lower + start, lower + end


def _opening_expression_tail_is_supported(
    text: str,
    *,
    expression: TemporalExpression,
    segment: _SegmentRange,
    expressions: tuple[TemporalExpression, ...],
) -> bool:
    if (
        _INTAKE_POST_EXPRESSION_BOUNDARY_RE.fullmatch(
            text[expression.end : segment.end]
        )
        is not None
    ):
        return True
    for closing in _EVENT_PREDICATE_RE.finditer(
        text,
        expression.end,
        segment.end,
    ):
        if _CLOSING_PREDICATE_RE.fullmatch(closing.group(0)) is None:
            continue
        if (
            _OPENING_TO_CLOSING_COORDINATION_RE.fullmatch(
                text[expression.end : closing.start()]
            )
            is None
        ):
            continue
        for closing_expression in expressions:
            gap = closing_expression.start - closing.end()
            if (
                closing_expression.field == expression.field
                and closing_expression.segment_id == expression.segment_id
                and closing_expression.end <= segment.end
                and 0 <= gap <= 24
                and _PREDICATE_EXPRESSION_LINK_RE.fullmatch(
                    text[closing.end() : closing_expression.start]
                )
                is not None
                and (
                    _INTAKE_POST_EXPRESSION_BOUNDARY_RE.fullmatch(
                        text[closing_expression.end : segment.end]
                    )
                    is not None
                    or _INTAKE_TRAILING_INDEPENDENT_CLAUSE_RE.match(
                        text[closing_expression.end : segment.end]
                    )
                    is not None
                )
            ):
                return True
    return False


def _predicate_has_forward_expression(
    text: str,
    *,
    fields: tuple[_FieldRange, ...],
    segments: tuple[_SegmentRange, ...],
    expressions: tuple[TemporalExpression, ...],
    predicate_start: int,
    link_end: int,
    require_terminal_expression: bool = False,
) -> bool:
    field = _field_for_span(fields, predicate_start, link_end)
    segment = next(
        (
            item
            for item in segments
            if item.start <= predicate_start and link_end <= item.end
        ),
        None,
    )
    if segment is None or segment.field != field:
        return False
    for expression in expressions:
        gap = expression.start - link_end
        if (
            expression.field == field
            and expression.segment_id == segment.segment_id
            and segment.start <= expression.start
            and expression.end <= segment.end
            and 0 <= gap <= 24
            and _PREDICATE_EXPRESSION_LINK_RE.fullmatch(
                text[link_end : expression.start]
            )
            is not None
            and (
                not require_terminal_expression
                or _INTAKE_POST_EXPRESSION_BOUNDARY_RE.fullmatch(
                    text[expression.end : segment.end]
                )
                is not None
            )
        ):
            return True
    return False


def _intake_boundary_subject_context(
    text: str,
    *,
    fields: tuple[_FieldRange, ...],
    field_name: TemporalField,
    predicate_start: int,
) -> bool:
    if (
        _third_person_intake_opening_subject_span(
            text,
            fields=fields,
            field_name=field_name,
            predicate_start=predicate_start,
        )
        is not None
    ):
        return True
    field = next((item for item in fields if item.name == field_name), None)
    lower, _upper = _clause_window(
        text,
        predicate_start,
        predicate_start,
        padding=100,
        lower_bound=field.start if field else 0,
        upper_bound=field.end if field else len(text),
    )
    subject = _CLOSURE_SUBJECT_SEPARATOR_RE.split(text[lower:predicate_start])[-1]
    subject = re.sub(
        r"\A(?:our|the|your)[ \t]+",
        "",
        subject.strip(),
        flags=re.IGNORECASE,
    )
    return _DEADLINE_CLOSURE_SUBJECT_RE.fullmatch(subject) is not None


def _effective_boundary_subject_context(
    text: str,
    *,
    fields: tuple[_FieldRange, ...],
    field_name: TemporalField,
    predicate_start: int,
) -> bool:
    field = next((item for item in fields if item.name == field_name), None)
    lower, _upper = _clause_window(
        text,
        predicate_start,
        predicate_start,
        padding=100,
        lower_bound=field.start if field else 0,
        upper_bound=field.end if field else len(text),
    )
    subject = _CLOSURE_SUBJECT_SEPARATOR_RE.split(text[lower:predicate_start])[-1]
    subject = re.sub(
        r"\A(?:our|the|your)[ \t]+",
        "",
        subject.strip(),
        flags=re.IGNORECASE,
    )
    return _EFFECTIVE_BOUNDARY_SUBJECT_RE.fullmatch(subject) is not None


def _trim_event_title_span(
    text: str,
    start: int,
    end: int,
) -> tuple[int, int] | None:
    while start < end and text[start] in " \t\"'([{<-–—|":
        start += 1
    while end > start and text[end - 1] in " \t\"')]}>,–—|:;":
        end -= 1
    if start >= end:
        return None
    prefix = _SUBJECT_REPLY_PREFIX_RE.match(text[start:end])
    if prefix is not None:
        start += prefix.end()
        while start < end and text[start].isspace():
            start += 1
    return (start, end) if start < end else None


def _event_title_span_is_eligible(
    text: str,
    span: tuple[int, int],
) -> bool:
    start, end = span
    value = text[start:end].strip()
    if not 3 <= len(value) <= 160:
        return False
    if _GENERIC_EVENT_TITLE_RE.fullmatch(value):
        return False
    if not re.search(r"[A-Za-z][A-Za-z]", value):
        return False
    if not re.search(
        r"[A-Za-z]", re.sub(r"\b(?:am|pm|utc|gmt)\b", "", value, flags=re.IGNORECASE)
    ):
        return False
    # A structured proper title is a more useful event-identity candidate than
    # the generic event noun it may contain (for example, "Orchid Interview"
    # versus "Interview").  Preserve both endpoints for review instead of
    # deleting the specific title merely because their spans overlap.
    return True


def _span_overlaps_expressions(
    span: tuple[int, int],
    expressions: tuple[TemporalExpression, ...],
) -> bool:
    return any(
        expression.start < span[1] and span[0] < expression.end
        for expression in expressions
    )


def _associate(
    text: str,
    expressions: tuple[TemporalExpression, ...],
    mentions: tuple[TemporalMention, ...],
    anchor: datetime | None,
    chunk_id: str | None,
    scope_prefix: str,
) -> tuple[tuple[TemporalLead, ...], int, bool, int, int]:
    edge_mentions = tuple(
        item
        for item in mentions
        if item.mention_type
        in {
            "event",
            "event_predicate",
            "deadline",
            "action",
            "boundary",
            "lifecycle",
            "structural_label",
            "event_title_candidate",
        }
    )
    # Keep the proven strict grammar on the exact mention vocabulary it already
    # used. New inventory types are review hints and cannot displace strict/direct
    # event or deadline endpoints.
    strict_mentions = tuple(
        item
        for item in edge_mentions
        if item.mention_type in _STRICT_ASSOCIATION_MENTION_TYPES
    )
    raw: list[
        tuple[
            TemporalExpression,
            TemporalMention,
            AssociationMode,
            TemporalRelation | None,
            TemporalKind | None,
            tuple[str, ...],
        ]
    ] = []
    paired: set[tuple[str, str]] = set()
    candidate_edge_count = 0
    broad_expressions = _spatially_bounded(
        expressions, _MAX_BROAD_ASSOCIATION_EXPRESSIONS
    )
    broad_edge_mentions = _spatially_bounded(
        edge_mentions, _MAX_BROAD_ASSOCIATION_MENTIONS
    )
    association_inventory_truncated = len(broad_expressions) != len(expressions) or len(
        broad_edge_mentions
    ) != len(edge_mentions)
    omitted_expression_count = len(expressions) - len(broad_expressions)
    omitted_mention_count = len(edge_mentions) - len(broad_edge_mentions)
    graph_truncated = association_inventory_truncated

    if anchor is not None:
        strict = discover_gmail_temporal_candidates(
            text=text,
            message_internal_at=anchor,
            chunk_id=chunk_id,
        )
        for candidate in strict:
            expression = _expression_for_span(
                expressions,
                candidate.expression_span.start,
                candidate.expression_span.end,
            )
            if expression is None:
                continue
            mention = _mention_for_strict_candidate(
                strict_mentions,
                text=text,
                expression=expression,
                relation=candidate.relation,
                cue_start=candidate.cue_span.start,
                cue_end=candidate.cue_span.end,
                field=expression.field,
            )
            if mention is None:
                continue
            blockers = _association_blockers(
                expression, mention, mentions, mode="direct_grammar"
            )
            raw.append(
                (
                    expression,
                    mention,
                    "direct_grammar",
                    candidate.relation,
                    candidate.kind,
                    blockers,
                )
            )
            paired.add((expression.expression_id, mention.mention_id))

    direct_predicate_expression_ids: dict[str, str] = {}
    for expression, mention in _direct_predicate_expression_pairs(
        text,
        broad_expressions,
        broad_edge_mentions,
    ):
        pair = (expression.expression_id, mention.mention_id)
        direct_predicate_expression_ids[mention.mention_id] = expression.expression_id
        if pair in paired:
            continue
        relation, kind = _relation_and_kind(text, expression, mention)
        blockers = _association_blockers(
            expression,
            mention,
            mentions,
            mode="direct_grammar",
        )
        raw.append(
            (
                expression,
                mention,
                "direct_grammar",
                relation,
                kind,
                blockers,
            )
        )
        paired.add(pair)
    candidate_edge_count += len(raw)

    local_edges: list[tuple[int, TemporalExpression, TemporalMention]] = []
    for expression in broad_expressions:
        for mention in broad_edge_mentions:
            pair = (expression.expression_id, mention.mention_id)
            if (
                pair in paired
                or expression.field != mention.field
                or (
                    mention.mention_type == "event_predicate"
                    and mention.mention_id in direct_predicate_expression_ids
                    and direct_predicate_expression_ids[mention.mention_id]
                    != expression.expression_id
                )
                or not _source_bound_title_pair_is_authorized(
                    text,
                    expressions,
                    expression,
                    mention,
                )
            ):
                continue
            gap, between = _association_gap(text, expression, mention)
            if gap > _MAX_LOCAL_ASSOCIATION_GAP:
                continue
            if _FORBIDDEN_ASSOCIATION_PUNCTUATION_RE.search(between):
                continue
            # HTML-to-text layout inserts both single and repeated newlines inside
            # otherwise local fields. Field isolation and sentence punctuation,
            # rather than layout whitespace, define this review-only edge.
            local_edges.append((gap, expression, mention))

    retained_local_edges, local_truncated = _retain_mutual_nearest_edges(
        local_edges, limit=_MAX_RETAINED_LOCAL_EDGES
    )
    candidate_edge_count += len(local_edges)
    graph_truncated = graph_truncated or local_truncated
    expression_edge_counts, mention_edge_counts = _association_edge_counts(local_edges)
    core_expression_edge_counts, _core_mention_edge_counts = _association_edge_counts(
        [
            edge
            for edge in local_edges
            if edge[2].mention_type in _CORE_ASSOCIATION_MENTION_TYPES
        ]
    )
    for _gap, expression, mention in retained_local_edges:
        pair = (expression.expression_id, mention.mention_id)
        relation, kind = _relation_and_kind(text, expression, mention)
        blockers = _association_blockers(
            expression, mention, mentions, mode="field_local"
        )
        blockers = _ordered_unique(
            (
                *blockers,
                *(
                    ("association_inventory_truncated",)
                    if association_inventory_truncated
                    else ()
                ),
                *(
                    ("multiple_association_mentions",)
                    if _expression_association_count(
                        expression,
                        mention,
                        all_counts=expression_edge_counts,
                        core_counts=core_expression_edge_counts,
                    )
                    > 1
                    else ()
                ),
                *(
                    ("multiple_association_expressions",)
                    if mention_edge_counts.get(mention.mention_id, 0) > 1
                    else ()
                ),
            )
        )
        raw.append(
            (
                expression,
                mention,
                "field_local",
                relation,
                kind,
                blockers,
            )
        )
        paired.add(pair)

    # Same-field associations beyond the tight grammar window, or across sentence
    # punctuation, are useful retrieval hints but never resolved claims. They are
    # both distance-bounded and degree/cap-bounded.
    near_edges: list[tuple[int, TemporalExpression, TemporalMention]] = []
    for expression in broad_expressions:
        for mention in broad_edge_mentions:
            pair = (expression.expression_id, mention.mention_id)
            if (
                pair in paired
                or expression.field != mention.field
                or (
                    mention.mention_type == "event_predicate"
                    and mention.mention_id in direct_predicate_expression_ids
                    and direct_predicate_expression_ids[mention.mention_id]
                    != expression.expression_id
                )
                or not _source_bound_title_pair_is_authorized(
                    text,
                    expressions,
                    expression,
                    mention,
                )
            ):
                continue
            gap, between = _association_gap(text, expression, mention)
            if gap > _MAX_NEAR_ASSOCIATION_GAP:
                continue
            if (
                gap <= _MAX_LOCAL_ASSOCIATION_GAP
                and not _FORBIDDEN_ASSOCIATION_PUNCTUATION_RE.search(between)
            ):
                continue
            near_edges.append((gap, expression, mention))

    retained_near_edges, near_truncated = _retain_mutual_nearest_edges(
        near_edges, limit=_MAX_RETAINED_NEAR_EDGES
    )
    candidate_edge_count += len(near_edges)
    graph_truncated = graph_truncated or near_truncated
    near_expression_counts, near_mention_counts = _association_edge_counts(near_edges)
    near_core_expression_counts, _near_core_mention_counts = _association_edge_counts(
        [
            edge
            for edge in near_edges
            if edge[2].mention_type in _CORE_ASSOCIATION_MENTION_TYPES
        ]
    )
    for _gap, expression, mention in retained_near_edges:
        pair = (expression.expression_id, mention.mention_id)
        relation, kind = _relation_and_kind(text, expression, mention)
        blockers = _ordered_unique(
            (
                *_association_blockers(
                    expression, mention, mentions, mode="field_near"
                ),
                "field_near_review_only",
                *(
                    ("association_inventory_truncated",)
                    if association_inventory_truncated
                    else ()
                ),
                *(
                    ("multiple_association_mentions",)
                    if _expression_association_count(
                        expression,
                        mention,
                        all_counts=near_expression_counts,
                        core_counts=near_core_expression_counts,
                    )
                    > 1
                    else ()
                ),
                *(
                    ("multiple_association_expressions",)
                    if near_mention_counts.get(mention.mention_id, 0) > 1
                    else ()
                ),
            )
        )
        raw.append(
            (
                expression,
                mention,
                "field_near",
                relation,
                kind,
                blockers,
            )
        )
        paired.add(pair)

    subject_mentions = tuple(
        item for item in broad_edge_mentions if item.field == "subject"
    )
    if len(expressions) == 1 and len(subject_mentions) == 1 and len(edge_mentions) == 1:
        expression = expressions[0]
        mention = subject_mentions[0]
        pair = (expression.expression_id, mention.mention_id)
        if pair not in paired:
            relation, kind = _relation_and_kind(text, expression, mention)
            blockers = _association_blockers(
                expression, mention, mentions, mode="subject_singleton"
            )
            raw.append(
                (
                    expression,
                    mention,
                    "subject_singleton",
                    relation,
                    kind,
                    blockers,
                )
            )
            paired.add(pair)
            candidate_edge_count += 1

    # A subject often names the event while the body carries several dates. Keep
    # these alternatives explicit and bounded instead of requiring a singleton.
    bridge_edges: list[tuple[int, TemporalExpression, TemporalMention]] = []
    for expression in broad_expressions:
        if expression.field != "body":
            continue
        for mention in subject_mentions:
            pair = (expression.expression_id, mention.mention_id)
            if pair in paired or not _source_bound_title_pair_is_authorized(
                text,
                expressions,
                expression,
                mention,
            ):
                continue
            gap, _between = _association_gap(text, expression, mention)
            bridge_edges.append((gap, expression, mention))

    retained_bridge_edges, bridge_truncated = _retain_mutual_nearest_edges(
        bridge_edges, limit=_MAX_RETAINED_BRIDGE_EDGES
    )
    candidate_edge_count += len(bridge_edges)
    graph_truncated = graph_truncated or bridge_truncated
    bridge_expression_counts, bridge_mention_counts = _association_edge_counts(
        bridge_edges
    )
    bridge_core_expression_counts, _bridge_core_mention_counts = (
        _association_edge_counts(
            [
                edge
                for edge in bridge_edges
                if edge[2].mention_type in _CORE_ASSOCIATION_MENTION_TYPES
            ]
        )
    )
    for _gap, expression, mention in retained_bridge_edges:
        pair = (expression.expression_id, mention.mention_id)
        relation, kind = _relation_and_kind(text, expression, mention)
        blockers = _ordered_unique(
            (
                *_association_blockers(
                    expression, mention, mentions, mode="subject_body_bridge"
                ),
                "subject_body_bridge_review_only",
                *(
                    ("association_inventory_truncated",)
                    if association_inventory_truncated
                    else ()
                ),
                *(
                    ("multiple_association_mentions",)
                    if _expression_association_count(
                        expression,
                        mention,
                        all_counts=bridge_expression_counts,
                        core_counts=bridge_core_expression_counts,
                    )
                    > 1
                    else ()
                ),
                *(
                    ("multiple_association_expressions",)
                    if bridge_mention_counts.get(mention.mention_id, 0) > 1
                    else ()
                ),
            )
        )
        raw.append(
            (
                expression,
                mention,
                "subject_body_bridge",
                relation,
                kind,
                blockers,
            )
        )
        paired.add(pair)

    fallback_edges = 0
    if len(expressions) == 1 and len(edge_mentions) == 1:
        expression = expressions[0]
        mention = edge_mentions[0]
        pair = (expression.expression_id, mention.mention_id)
        if pair not in paired and _source_bound_title_pair_is_authorized(
            text,
            expressions,
            expression,
            mention,
        ):
            relation, kind = _relation_and_kind(text, expression, mention)
            blockers = _association_blockers(
                expression, mention, mentions, mode="message_singleton"
            )
            raw.append(
                (
                    expression,
                    mention,
                    "message_singleton",
                    relation,
                    kind,
                    blockers,
                )
            )
            fallback_edges += 1

    mode_rank = {
        "direct_grammar": 0,
        "field_local": 1,
        "field_near": 2,
        "subject_singleton": 3,
        "subject_body_bridge": 4,
        "message_singleton": 5,
    }
    raw.sort(
        key=lambda item: (
            item[0].start,
            item[1].start,
            mode_rank[item[2]],
        )
    )
    leads = tuple(
        TemporalLead(
            lead_id=f"{scope_prefix}:l{index}",
            expression_id=expression.expression_id,
            mention_id=mention.mention_id,
            association_mode=mode,
            relation=relation,
            kind=kind,
            confidence_tier=_confidence_tier(mode, blockers),
            gap_chars=_association_gap(text, expression, mention)[0],
            blockers=blockers,
            risk_features=_association_risk_features(text, expression, mention, mode),
        )
        for index, (
            expression,
            mention,
            mode,
            relation,
            kind,
            blockers,
        ) in enumerate(raw, start=1)
    )
    candidate_edge_count += fallback_edges
    return (
        leads,
        candidate_edge_count,
        graph_truncated,
        omitted_expression_count,
        omitted_mention_count,
    )


def _spatially_bounded(items: tuple[_T, ...], limit: int) -> tuple[_T, ...]:
    """Bound hint construction while retaining deterministic mailbox coverage."""

    if len(items) <= limit:
        return items
    if limit <= 1:
        return items[:1]
    indexes = {(index * (len(items) - 1)) // (limit - 1) for index in range(limit)}
    return tuple(items[index] for index in sorted(indexes))


def _retain_mutual_nearest_edges(
    edges: list[tuple[int, TemporalExpression, TemporalMention]],
    *,
    limit: int,
) -> tuple[list[tuple[int, TemporalExpression, TemporalMention]], bool]:
    """Keep bounded mutually near alternatives at each endpoint."""

    by_expression: dict[str, list[tuple[int, TemporalExpression, TemporalMention]]] = {}
    by_mention: dict[str, list[tuple[int, TemporalExpression, TemporalMention]]] = {}
    for edge in edges:
        by_expression.setdefault(edge[1].expression_id, []).append(edge)
        by_mention.setdefault(edge[2].mention_id, []).append(edge)

    top_for_expression: set[tuple[str, str]] = set()
    top_for_mention: set[tuple[str, str]] = set()
    for values in by_expression.values():
        ranked = sorted(
            values,
            key=lambda edge: (
                0
                if GMAIL_TEMPORAL_CLAUSE_BOUND_EVENT_TITLE_BLOCKER in edge[2].blockers
                else 1,
                edge[0],
                edge[2].start,
                edge[2].mention_id,
            ),
        )[:2]
        top_for_expression.update(
            (edge[1].expression_id, edge[2].mention_id) for edge in ranked
        )
    for values in by_mention.values():
        mention_limit = (
            4
            if GMAIL_TEMPORAL_CLAUSE_BOUND_EVENT_TITLE_BLOCKER in values[0][2].blockers
            else 2
        )
        ranked = sorted(
            values,
            key=lambda edge: (edge[0], edge[1].start, edge[1].expression_id),
        )[:mention_limit]
        top_for_mention.update(
            (edge[1].expression_id, edge[2].mention_id) for edge in ranked
        )
    retained = sorted(
        (
            edge
            for edge in edges
            if (edge[1].expression_id, edge[2].mention_id) in top_for_expression
            and (edge[1].expression_id, edge[2].mention_id) in top_for_mention
        ),
        key=lambda edge: (edge[1].start, edge[2].start, edge[0]),
    )[:limit]
    return retained, len(retained) < len(edges)


def _association_edge_counts(
    edges: list[tuple[int, TemporalExpression, TemporalMention]],
) -> tuple[dict[str, int], dict[str, int]]:
    expression_counts: dict[str, int] = {}
    mention_counts: dict[str, int] = {}
    for _gap, expression, mention in edges:
        expression_counts[expression.expression_id] = (
            expression_counts.get(expression.expression_id, 0) + 1
        )
        mention_counts[mention.mention_id] = (
            mention_counts.get(mention.mention_id, 0) + 1
        )
    return expression_counts, mention_counts


def _expression_association_count(
    expression: TemporalExpression,
    mention: TemporalMention,
    *,
    all_counts: dict[str, int],
    core_counts: dict[str, int],
) -> int:
    """Do not let newly admitted review cues downgrade proven core edges."""

    counts = (
        core_counts
        if mention.mention_type in _CORE_ASSOCIATION_MENTION_TYPES
        else all_counts
    )
    return counts.get(expression.expression_id, 0)


def _association_blockers(
    expression: TemporalExpression,
    mention: TemporalMention,
    mentions: tuple[TemporalMention, ...],
    *,
    mode: AssociationMode,
) -> tuple[str, ...]:
    blockers: list[str] = [*expression.blockers, *mention.blockers]
    if len(expression.normalized_options) != 1:
        blockers.append("normalization_not_single_complete_value")
    elif not _complete_normalization_is_valid(expression.normalized_options[0]):
        blockers.append("invalid_normalized_value")
    pair_start = min(expression.start, mention.start)
    pair_end = max(expression.end, mention.end)
    cross_field_subject_context = (
        mode in {"subject_body_bridge", "subject_singleton"}
        and expression.field == "body"
        and mention.field == "subject"
    )
    for other in mentions:
        if other.field != expression.field:
            continue
        if cross_field_subject_context:
            # The subject names the candidate event, but contextual blockers
            # belong to the sentence containing the body expression. Requiring
            # the subject endpoint to share that segment makes every bridge
            # context-free; allowing other body segments leaks stale lifecycle
            # or artifact semantics across sentences.
            same_context_segment = other.segment_id == expression.segment_id
        else:
            same_context_segment = (
                expression.segment_id == mention.segment_id == other.segment_id
            )
        if not same_context_segment:
            continue
        distance = _span_distance(pair_start, pair_end, other.start, other.end)
        if mode == "direct_grammar":
            in_scope = pair_start <= other.start and other.end <= pair_end
        elif mode in {"field_near", "subject_body_bridge"}:
            in_scope = (
                pair_start <= other.start and other.end <= pair_end
            ) or distance <= _MAX_LOCAL_ASSOCIATION_GAP
        else:
            in_scope = distance <= _MAX_LOCAL_ASSOCIATION_GAP
        if not in_scope:
            continue
        if other.mention_type == "artifact" and not (
            mode == "direct_grammar" and mention.mention_type == "action"
        ):
            blockers.extend(other.blockers or ("artifact_context",))
        elif other.mention_type == "boundary":
            blockers.extend(
                other.blockers or ("terminal_boundary_not_occurrence_start",)
            )
        elif other.mention_type == "lifecycle" and other.blockers:
            blockers.extend(other.blockers)
    return _ordered_unique(blockers)


def _relation_and_kind(
    text: str,
    expression: TemporalExpression,
    mention: TemporalMention,
) -> tuple[TemporalRelation | None, TemporalKind | None]:
    start, end = _clause_window(
        text,
        min(expression.start, mention.start),
        max(expression.end, mention.end),
        padding=20,
    )
    association_text = text[start:end]
    relation = mention.relation
    if mention.mention_type == "action":
        relation = (
            "deadline"
            if _action_deadline_association_supported(
                text,
                expression=expression,
                mention=mention,
                window_start=start,
                window_end=end,
            )
            else None
        )
    kind = mention.kind
    if kind is None:
        planned = bool(_PLANNED_CONTEXT_RE.search(association_text))
        actual = bool(_ACTUAL_CONTEXT_RE.search(association_text))
        if planned != actual:
            kind = "planned" if planned else "actual"
        elif relation == "deadline":
            kind = "planned"
    return relation, kind


def _action_deadline_association_supported(
    text: str,
    *,
    expression: TemporalExpression,
    mention: TemporalMention,
    window_start: int,
    window_end: int,
) -> bool:
    """Distinguish a deadline connector from an agentive ``by`` phrase."""

    association_text = text[window_start:window_end]
    if re.search(
        r"\b(?:before|deadline|due|no\s+later\s+than|until)\b",
        association_text,
        re.IGNORECASE,
    ):
        return True
    for match in re.finditer(r"\bby\b", association_text, re.IGNORECASE):
        cue_start = window_start + match.start()
        cue_end = window_start + match.end()
        if mention.end <= cue_start <= expression.start:
            action_to_cue = text[mention.end : cue_start]
            if (
                _FORBIDDEN_ASSOCIATION_PUNCTUATION_RE.search(action_to_cue) is None
                and _COORDINATING_CLAUSE_BOUNDARY_RE.search(action_to_cue) is None
                and _action_by_expression_gap_supported(
                    text[cue_end : expression.start]
                )
            ):
                return True
        elif cue_end <= expression.start <= expression.end <= mention.start:
            # Preserve the established date-first grammar: ``By DATE, submit``.
            if (
                _action_by_expression_gap_supported(text[cue_end : expression.start])
                and re.fullmatch(
                    r"[ \t]*,?[ \t]*", text[expression.end : mention.start]
                )
                is not None
            ):
                return True
    return False


def _action_by_expression_gap_supported(value: str) -> bool:
    if len(value) > 180 or re.search(r"[;.!?\n]", value):
        return False
    if re.search(
        r"\b(?:january|february|march|april|may|june|july|august|"
        r"september|october|november|december)\.?\s+\d{1,2}"
        r"|\b\d{4}-\d{2}-\d{2}\b|\b(?:today|tomorrow)\b",
        value,
        re.IGNORECASE,
    ):
        return False
    gap = value.strip(" \t\r\n,;:-")
    if not gap:
        return True
    return (
        re.fullmatch(
            r"(?:(?:the\s+)?(?:deadline|due\s+date)(?:\s+is)?|"
            r"(?:(?:the\s+)?(?:end\s+of\s+day|close\s+of\s+business)|eod|cob)"
            r"(?:\s+on)?|"
            r"(?:on\s+)?(?:monday|tuesday|wednesday|thursday|friday|"
            r"saturday|sunday))",
            gap,
            re.IGNORECASE,
        )
        is not None
    )


def _expression_for_span(
    expressions: tuple[TemporalExpression, ...], start: int, end: int
) -> TemporalExpression | None:
    exact = [item for item in expressions if item.start == start and item.end == end]
    if exact:
        return exact[0]
    overlapping = [
        item for item in expressions if item.start < end and start < item.end
    ]
    return min(
        overlapping,
        key=lambda item: (abs(item.start - start) + abs(item.end - end), item.start),
        default=None,
    )


def _mention_for_strict_candidate(
    mentions: tuple[TemporalMention, ...],
    *,
    text: str,
    expression: TemporalExpression,
    relation: str,
    cue_start: int,
    cue_end: int,
    field: TemporalField,
) -> TemporalMention | None:
    compatible = [
        item for item in mentions if item.field == field and item.relation == relation
    ]
    cue_surface = text[cue_start:cue_end].strip()
    if relation == "deadline" and _DIRECT_ACTION_DEADLINE_CUE_RE.fullmatch(cue_surface):
        action_candidates = [
            item
            for item in mentions
            if item.mention_type == "action"
            and item.field == field
            and item.segment_id == expression.segment_id
            and item.end <= cue_start
            and cue_start - item.end <= 90
            and _FORBIDDEN_ASSOCIATION_PUNCTUATION_RE.search(text[item.end : cue_start])
            is None
            and _COORDINATING_CLAUSE_BOUNDARY_RE.search(text[item.end : cue_start])
            is None
        ]
        if action_candidates:
            return min(
                action_candidates,
                key=lambda item: _span_distance(
                    item.start,
                    item.end,
                    cue_start,
                    cue_end,
                ),
            )
    return min(
        compatible,
        key=lambda item: _span_distance(item.start, item.end, cue_start, cue_end),
        default=None,
    )


def _direct_predicate_expression_pairs(
    text: str,
    expressions: tuple[TemporalExpression, ...],
    mentions: tuple[TemporalMention, ...],
) -> tuple[tuple[TemporalExpression, TemporalMention], ...]:
    """Bind an opening or closing predicate only to its adjacent expression."""

    output: list[tuple[TemporalExpression, TemporalMention]] = []
    for mention in mentions:
        surface = text[mention.start : mention.end]
        if mention.mention_type != "event_predicate" or not (
            _OPENING_PREDICATE_RE.fullmatch(surface)
            or _CLOSING_PREDICATE_RE.fullmatch(surface)
        ):
            continue
        adjacent = tuple(
            sorted(
                (
                    expression
                    for expression in expressions
                    if expression.field == mention.field
                    and expression.segment_id == mention.segment_id
                    and expression.start >= mention.end
                    and _PREDICATE_EXPRESSION_LINK_RE.fullmatch(
                        text[mention.end : expression.start]
                    )
                    is not None
                ),
                key=lambda expression: (expression.start, expression.end),
            )
        )
        if not adjacent:
            continue
        first = adjacent[0]
        output.append((first, mention))
        previous = first
        for expression in sorted(
            (
                item
                for item in expressions
                if item.field == mention.field
                and item.segment_id == mention.segment_id
                and item.start >= first.end
            ),
            key=lambda item: (item.start, item.end),
        ):
            if (
                _PREDICATE_ADDITIONAL_EXPRESSION_LINK_RE.fullmatch(
                    text[previous.end : expression.start]
                )
                is None
            ):
                break
            output.append((expression, mention))
            previous = expression
    return tuple(output)


def _association_gap(
    text: str, expression: TemporalExpression, mention: TemporalMention
) -> tuple[int, str]:
    if expression.end <= mention.start:
        return mention.start - expression.end, text[expression.end : mention.start]
    if mention.end <= expression.start:
        return expression.start - mention.end, text[mention.end : expression.start]
    return 0, ""


def _source_bound_title_pair_is_authorized(
    text: str,
    expressions: tuple[TemporalExpression, ...],
    expression: TemporalExpression,
    mention: TemporalMention,
) -> bool:
    """Keep a rescued title on only the expressions its source frame names."""

    intake_opening = _source_bound_intake_opening_pair_authorization(
        text,
        expressions=expressions,
        expression=expression,
        mention=mention,
    )
    if intake_opening is not None:
        return intake_opening
    if GMAIL_TEMPORAL_CLAUSE_BOUND_EVENT_TITLE_BLOCKER not in mention.blockers:
        return True
    matches = tuple(
        match
        for match in _SOURCE_BOUND_EVENT_TITLE_RE.finditer(text)
        if match.span("title") == (mention.start, mention.end)
    )
    if len(matches) != 1:
        return False
    match = matches[0]
    frame_expressions = tuple(
        sorted(
            (
                candidate
                for candidate in expressions
                if candidate.field == mention.field
                and candidate.segment_id == mention.segment_id
                and candidate.start >= match.end("predicate")
            ),
            key=lambda candidate: (candidate.start, candidate.end),
        )
    )
    if not frame_expressions:
        return False
    first = frame_expressions[0]
    if (
        _SOURCE_BOUND_FIRST_EXPRESSION_LINK_RE.fullmatch(
            text[match.end("predicate") : first.start]
        )
        is None
    ):
        return False

    authorized = {first.expression_id}
    predicate = match.group("predicate").casefold()
    if "rescheduled" in predicate and len(frame_expressions) > 1:
        first_role = predicate.rsplit(maxsplit=1)[-1]
        second = frame_expressions[1]
        second_link = _SOURCE_BOUND_RESCHEDULE_NEXT_LINK_RE.fullmatch(
            text[first.end : second.start]
        )
        if (
            second_link is not None
            and second_link.group("role").casefold() != first_role
        ):
            authorized.add(second.expression_id)
    elif predicate.startswith("may "):
        previous = first
        for candidate in frame_expressions[1:4]:
            if (
                _SOURCE_BOUND_OPTION_NEXT_LINK_RE.fullmatch(
                    text[previous.end : candidate.start]
                )
                is None
            ):
                break
            authorized.add(candidate.expression_id)
            previous = candidate
    return expression.expression_id in authorized


def _source_bound_intake_opening_pair_authorization(
    text: str,
    *,
    expressions: tuple[TemporalExpression, ...],
    expression: TemporalExpression,
    mention: TemporalMention,
) -> bool | None:
    """Confine a synthesized intake event subject to its opening endpoint."""

    if mention.mention_type != "event":
        return None
    predicate = re.match(
        r"[ \t]+(?P<cue>opens?)(?:[ \t]+(?:at|on))?[ \t]*",
        text[mention.end :],
        re.IGNORECASE,
    )
    if predicate is None:
        return None
    predicate_start = mention.end + predicate.start("cue")
    subject_span = _third_person_intake_opening_subject_span(
        text,
        fields=_field_ranges(text),
        field_name=mention.field,
        predicate_start=predicate_start,
    )
    if subject_span != (mention.start, mention.end):
        return None
    predicate_end = mention.end + predicate.end()
    right_hand = tuple(
        sorted(
            (
                candidate
                for candidate in expressions
                if candidate.field == mention.field
                and candidate.segment_id == mention.segment_id
                and candidate.start >= predicate_end
            ),
            key=lambda candidate: (candidate.start, candidate.end),
        )
    )
    if not right_hand:
        return False
    first = right_hand[0]
    if (
        _PREDICATE_EXPRESSION_LINK_RE.fullmatch(text[predicate_end : first.start])
        is None
    ):
        return False
    return expression.expression_id == first.expression_id


def _confidence_tier(
    mode: AssociationMode, blockers: tuple[str, ...]
) -> ConfidenceTier:
    if blockers:
        return "review_ambiguous"
    if mode == "direct_grammar":
        return "strict_direct"
    if mode == "field_local":
        return "review_resolved"
    return "review_fallback"


def _field_ranges(text: str) -> tuple[_FieldRange, ...]:
    if not text.startswith("Subject: "):
        return (_FieldRange("message", 0, len(text)),)
    line_end = text.find("\n")
    if line_end < 0:
        return (_FieldRange("subject", len("Subject: "), len(text)),)
    body_start = line_end
    while body_start < len(text) and text[body_start] == "\n":
        body_start += 1
    fields = [_FieldRange("subject", len("Subject: "), line_end)]
    if body_start < len(text):
        fields.append(_FieldRange("body", body_start, len(text)))
    return tuple(fields)


def _quoted_or_forwarded_ranges(text: str) -> tuple[tuple[int, int], ...]:
    """Return exact ranges whose temporal evidence is not newly authored.

    Individually quoted lines stay individually bounded.  A forwarded/original
    message marker changes the provenance of the marker and all content after
    it, so the first such marker starts one tail range.  Merging keeps overlap
    checks deterministic without discarding any endpoint inventory.
    """

    ranges = [(match.start(), match.end()) for match in _QUOTED_LINE_RE.finditer(text)]
    marker = _FORWARDED_ORIGINAL_MARKER_RE.search(text)
    if marker is not None:
        ranges.append((marker.start(), len(text)))
    if not ranges:
        return ()

    merged: list[tuple[int, int]] = []
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1]:
            previous_start, previous_end = merged[-1]
            merged[-1] = (previous_start, max(previous_end, end))
        else:
            merged.append((start, end))
    return tuple(merged)


def _superseded_historical_ranges(text: str) -> tuple[tuple[int, int], ...]:
    """Return matching historical clauses superseded by the authored update.

    A current-state denial only supersedes quoted history for the event named in
    the message subject.  Even within one forwarded tail, unrelated deadlines
    remain reviewable.  This conservative identity certificate intentionally
    prefers recall when the event cannot be matched from source text.
    """

    marker = _FORWARDED_ORIGINAL_MARKER_RE.search(text)
    if marker is None:
        return ()
    clause_matches = tuple(re.finditer(r"[^.!?;\r\n]+", text))
    authored_clauses = tuple(
        match
        for match in clause_matches
        if match.end() <= marker.start() and match.group().strip()
    )
    if (
        not authored_clauses
        or _NO_ACTIVE_DATE_CLAUSE_RE.fullmatch(authored_clauses[-1].group().strip())
        is None
    ):
        return ()
    history_start = marker.end()

    identity_tokens = _current_status_subject_identity_tokens(text)
    if not identity_tokens:
        return ()
    identity_pattern = re.compile(
        r"(?<!\w)"
        + r"(?:[\W_]+)".join(re.escape(token) for token in identity_tokens)
        + r"(?!\w)",
        re.IGNORECASE,
    )
    ranges: list[tuple[int, int]] = []
    for match in identity_pattern.finditer(text, history_start):
        boundary = re.search(r"[.!?;\r\n]", text[match.end() :])
        clause_end = len(text) if boundary is None else match.end() + boundary.end()
        schedule = _HISTORICAL_SCHEDULE_CLAUSE_RE.match(
            text,
            match.end(),
            clause_end,
        )
        if schedule is None:
            continue
        coordinator = _HISTORICAL_CONJUNCT_BOUNDARY_RE.search(
            text,
            schedule.end(),
            clause_end,
        )
        ranges.append(
            (
                match.start(),
                coordinator.start() if coordinator is not None else clause_end,
            )
        )
    return tuple(ranges)


def _current_status_subject_identity_tokens(text: str) -> tuple[str, ...]:
    """Return a distinctive event identity from an explicit status subject."""

    subject_match = re.search(r"(?im)^Subject:[ \t]*(?P<subject>[^\r\n]+)", text)
    if subject_match is None:
        return ()
    subject = _SUBJECT_REPLY_PREFIX_RE.sub("", subject_match.group("subject"))
    match = _CURRENT_STATUS_SUBJECT_IDENTITY_RE.fullmatch(subject.strip())
    if match is None:
        return ()
    tokens = tuple(
        token.casefold()
        for token in re.findall(r"[^\W_]+", match.group("identity"), re.UNICODE)
    )
    if len(tokens) < 2 or not any(
        token not in _GENERIC_STATUS_IDENTITY_TOKENS for token in tokens
    ):
        return ()
    return tokens


def _quoted_or_forwarded_blocker(
    start: int,
    end: int,
    ranges: tuple[tuple[int, int], ...],
) -> tuple[str, ...]:
    if any(
        range_start < end and start < range_end for range_start, range_end in ranges
    ):
        return ("quoted_or_forwarded_context",)
    return ()


def _superseded_historical_blocker(
    start: int,
    end: int,
    ranges: tuple[tuple[int, int], ...],
) -> tuple[str, ...]:
    if any(
        range_start < end and start < range_end for range_start, range_end in ranges
    ):
        return (GMAIL_TEMPORAL_SUPERSEDED_HISTORICAL_BLOCKER,)
    return ()


_SEGMENT_BREAK_RE = re.compile(
    r"\n[ \t]*\n+|;[ \t]*|[.!?](?:[ \t]+(?=[A-Z])|\n+)"
    r"|\n(?=[ \t]*(?:>|[-*\u2022][ \t]+|(?:begin[ \t]+)?forwarded[ \t]+message|"
    r"-{2,}[ \t]*original[ \t]+message[ \t]*-{2,}))",
    re.IGNORECASE,
)


def _segment_ranges(
    text: str,
    fields: tuple[_FieldRange, ...],
) -> tuple[_SegmentRange, ...]:
    """Partition fields into stable local clauses without dropping source text."""

    output: list[_SegmentRange] = []
    for field in fields:
        cursor = field.start
        index = 1
        for match in _SEGMENT_BREAK_RE.finditer(text, field.start, field.end):
            boundary = match.end()
            if boundary > cursor:
                output.append(
                    _SegmentRange(
                        segment_id=f"{field.name}:s{index}",
                        field=field.name,
                        start=cursor,
                        end=boundary,
                    )
                )
                index += 1
            cursor = boundary
        if cursor < field.end or not output:
            output.append(
                _SegmentRange(
                    segment_id=f"{field.name}:s{index}",
                    field=field.name,
                    start=cursor,
                    end=field.end,
                )
            )
    return tuple(output)


def _segment_id_for_span(
    segments: tuple[_SegmentRange, ...],
    start: int,
    end: int,
) -> str:
    for segment in segments:
        if segment.start <= start and end <= segment.end:
            return segment.segment_id
    midpoint = start + max(0, end - start) // 2
    nearest = min(
        segments,
        key=lambda item: (
            0 if item.start <= midpoint <= item.end else 1,
            min(abs(midpoint - item.start), abs(midpoint - item.end)),
            item.segment_id,
        ),
        default=None,
    )
    return nearest.segment_id if nearest is not None else "message:s1"


def _field_for_span(
    fields: tuple[_FieldRange, ...], start: int, end: int
) -> TemporalField:
    for field in fields:
        if field.start <= start and end <= field.end:
            return field.name
    return "message"


def _context_kind(
    text: str,
    fields: tuple[_FieldRange, ...],
    field_name: TemporalField,
    start: int,
    end: int,
) -> TemporalKind | None:
    field = next((item for item in fields if item.name == field_name), None)
    lower = max(field.start if field else 0, start - 60)
    upper = min(field.end if field else len(text), end + 60)
    lower, upper = _clause_window(
        text,
        start,
        end,
        padding=60,
        lower_bound=lower,
        upper_bound=upper,
    )
    context = text[lower:upper]
    planned = bool(_PLANNED_CONTEXT_RE.search(context))
    actual = bool(_ACTUAL_CONTEXT_RE.search(context))
    if planned == actual:
        return None
    return "planned" if planned else "actual"


def _deadline_closure_context(
    text: str,
    fields: tuple[_FieldRange, ...],
    field_name: TemporalField,
    start: int,
    end: int,
) -> bool:
    """Limit deadline semantics to a source-bound intake-window subject."""

    field = next((item for item in fields if item.name == field_name), None)
    lower_bound = field.start if field else 0
    upper_bound = field.end if field else len(text)
    lower, _upper = _clause_window(
        text,
        start,
        end,
        padding=100,
        lower_bound=lower_bound,
        upper_bound=upper_bound,
    )
    prefix = text[lower:start]
    if _deadline_closure_subject(prefix):
        return True

    # Preserve an explicitly shared subject in "Registration opens ... and
    # closes ...". The trailing coordinator must be empty of a new subject,
    # and the earlier predicate must itself be an opening transition.
    coordinator = _TRAILING_COORDINATION_RE.search(prefix)
    if coordinator is None:
        return False
    before_coordinator = prefix[: coordinator.start()]
    openings = tuple(_OPENING_PREDICATE_SCAN_RE.finditer(before_coordinator))
    if not openings:
        return False
    return _deadline_closure_subject(before_coordinator[: openings[-1].start()])


def _deadline_closure_subject(prefix: str) -> bool:
    """Return whether the final coordinated subject is an intake window."""

    subject = _CLOSURE_SUBJECT_SEPARATOR_RE.split(prefix)[-1].strip()
    if not subject:
        return False
    return _DEADLINE_CLOSURE_SUBJECT_RE.search(subject) is not None


def _numeric_order_dates(
    first: int, second: int, year: int
) -> tuple[tuple[date, ...], str]:
    values: list[date] = []
    if first <= 12:
        value = _safe_date(year, first, second)
        if value:
            values.append(value)
    if second <= 12:
        value = _safe_date(year, second, first)
        if value and value not in values:
            values.append(value)
    if len(values) > 1:
        basis = "numeric_date_ambiguous_month_day_order"
    elif first > 12:
        basis = "numeric_date_unambiguous_day_month_order"
    elif second > 12:
        basis = "numeric_date_unambiguous_month_day_order"
    else:
        basis = "numeric_date_equivalent_or_single_valid_order"
    return tuple(values), basis


def _numeric_order_dates_with_inferred_year(
    first: int, second: int, anchor: date | None
) -> tuple[tuple[date, ...], str]:
    if anchor is None:
        return (), "numeric_date_missing_year_and_anchor"
    values: list[date] = []
    if first <= 12:
        values.extend(_inferred_date_options(first, second, anchor))
    if second <= 12:
        for value in _inferred_date_options(second, first, anchor):
            if value not in values:
                values.append(value)
    return tuple(values), (
        "numeric_date_ambiguous_order_year_inferred_from_message_internal_at"
        if len(values) > 1
        else "numeric_date_year_inferred_from_message_internal_at"
    )


def _weekday_options(anchor: date, weekday: int, qualifier: str) -> tuple[date, ...]:
    forward = (weekday - anchor.weekday()) % 7
    backward = (anchor.weekday() - weekday) % 7
    if qualifier == "this":
        return (anchor + timedelta(days=forward),)
    if qualifier == "this coming":
        return (anchor + timedelta(days=forward or 7),)
    if qualifier == "last":
        return (anchor - timedelta(days=backward or 7),)
    if qualifier == "next":
        first = anchor + timedelta(days=forward or 7)
        return (first, first + timedelta(days=7))
    previous = anchor - timedelta(days=backward or 7)
    following = anchor + timedelta(days=forward or 7)
    return (previous, following)


def _inferred_date_options(
    month: int, day: int, anchor: date | None
) -> tuple[date, ...]:
    if anchor is None:
        return ()
    choices: list[tuple[int, date]] = []
    for year in (anchor.year - 1, anchor.year, anchor.year + 1):
        value = _safe_date(year, month, day)
        if value is None:
            continue
        distance = abs((value - anchor).days)
        if distance <= _MAX_YEAR_INFERENCE_DISTANCE:
            choices.append((distance, value))
    choices.sort(key=lambda item: (item[0], item[1]))
    if not choices:
        return ()
    # A sender's missing year is not made authoritative by choosing the nearest
    # calendar occurrence. Preserve the two closest plausible adjacent-year
    # interpretations and let association/review resolve them.
    return tuple(item[1] for item in choices[:2])


def _interval_options(
    first: date | None, last: date | None, connector: str
) -> tuple[str, ...]:
    if first is None or last is None or last <= first or (last - first).days > 366:
        return ()
    exclusive_end = (
        last if connector.strip().casefold() == "until" else last + timedelta(days=1)
    )
    return (f"{first.isoformat()}/{exclusive_end.isoformat()}",)


def _range_basis(connector: str) -> str:
    return (
        "exclusive_until_date_range"
        if connector.strip().casefold() == "until"
        else "inclusive_textual_date_range"
    )


def _clock_has_time(match: re.Match[str]) -> bool:
    return match.group("minute") is not None or match.group("ampm") is not None


def _clock_is_likely_non_temporal(text: str, match: re.Match[str]) -> bool:
    """Reject common identifiers/offset fragments without suppressing bare clocks."""

    previous = text[match.start() - 1 : match.start()]
    following = text[match.end() : match.end() + 1]
    if previous and (previous.isalnum() or previous in {"_", "/", "#", "+", "-"}):
        return True
    if following in {"/", "%", "_"}:
        return True
    prefix = text[max(0, match.start() - 24) : match.start()]
    return bool(
        re.search(
            r"(?:https?://|\b(?:code|identifier|ratio|reference|ref|version)\s*[:#-]?\s*)$",
            prefix,
            re.IGNORECASE,
        )
    )


def _timezone_for_text(
    value: str,
) -> tuple[timezone | ZoneInfo | None, str, str | None]:
    compact = value.strip().upper().replace(" ", "")
    if compact in {"Z", "UTC", "GMT"}:
        return timezone.utc, "explicit_utc_designator", None
    if compact in _NORTH_AMERICAN_ABBREVIATION_OFFSETS:
        return (
            timezone(timedelta(hours=_NORTH_AMERICAN_ABBREVIATION_OFFSETS[compact])),
            "fixed_north_american_timezone_abbreviation",
            "timezone_abbreviation_requires_review",
        )
    numeric = re.fullmatch(r"(?:(?:UTC|GMT))?([+-])(\d{1,2})(?::?(\d{2}))?", compact)
    if numeric:
        hours = int(numeric.group(2))
        minutes = int(numeric.group(3) or 0)
        if hours > 14 or minutes > 59 or (hours == 14 and minutes):
            return None, "invalid_numeric_utc_offset", "invalid_timezone"
        delta = timedelta(hours=hours, minutes=minutes)
        if numeric.group(1) == "-":
            delta = -delta
        return timezone(delta), "explicit_numeric_utc_offset", None
    try:
        return ZoneInfo(value), "explicit_iana_timezone", None
    except (ValueError, ZoneInfoNotFoundError):
        return None, "invalid_timezone", "invalid_timezone"


def _iana_local_time_is_unambiguous(
    day: date,
    hour: int,
    minute: int,
    second: int,
    microsecond: int,
    zone: ZoneInfo,
) -> bool:
    naive = datetime.combine(day, time(hour, minute, second, microsecond))
    offsets: set[timedelta | None] = set()
    for fold in (0, 1):
        localized = naive.replace(tzinfo=zone, fold=fold)
        round_trip = localized.astimezone(timezone.utc).astimezone(zone)
        if round_trip.replace(tzinfo=None) == naive:
            offsets.add(localized.utcoffset())
    return len(offsets) == 1


def _aware_internal_time(value: str | datetime | None) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif value is None:
        return None
    else:
        raw = str(value).strip()
        if re.fullmatch(r"\d{13}", raw):
            try:
                parsed = datetime.fromtimestamp(int(raw) / 1000, tz=timezone.utc)
            except (OverflowError, OSError, ValueError):
                return None
        else:
            try:
                parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except ValueError:
                return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed


def _date_from_match(match: re.Match[str]) -> date | None:
    try:
        return date(
            int(match.group("year")),
            _month_number(match.group("month")),
            int(match.group("day")),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _month_number(value: str) -> int:
    return _MONTHS[value.casefold().rstrip(".")]


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _calendar_date_options(values: tuple[str, ...]) -> tuple[str, ...]:
    output: list[str] = []
    for value in values:
        try:
            parsed = date.fromisoformat(value)
        except ValueError:
            continue
        output.append(parsed.isoformat())
    return tuple(dict.fromkeys(output))


def _complete_normalization_is_valid(value: str) -> bool:
    if "/" in value:
        first, separator, last = value.partition("/")
        if not separator:
            return False
        try:
            return date.fromisoformat(last) > date.fromisoformat(first)
        except ValueError:
            return False
    try:
        if "T" in value:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed.tzinfo is not None and parsed.utcoffset() is not None
        date.fromisoformat(value)
        return True
    except ValueError:
        return False


def _is_lifecycle_reassignment(
    text: str, first_expression_start: int, connector: str
) -> bool:
    if connector.strip().casefold() not in {"to", "until"}:
        return False
    clause_start = (
        max(
            text.rfind(".", 0, first_expression_start),
            text.rfind("?", 0, first_expression_start),
            text.rfind("!", 0, first_expression_start),
            text.rfind(";", 0, first_expression_start),
        )
        + 1
    )
    prefix = text[
        max(clause_start, first_expression_start - 120) : first_expression_start
    ]
    return bool(
        re.search(
            r"\b(?:changed|moved|postponed|rescheduled|pushed\s+back)\b"
            r"(?:\s+(?:again|once\s+more))?\s+from\s*$",
            prefix,
            re.IGNORECASE,
        )
    )


def _clause_window(
    text: str,
    start: int,
    end: int,
    *,
    padding: int,
    lower_bound: int = 0,
    upper_bound: int | None = None,
) -> tuple[int, int]:
    upper_limit = len(text) if upper_bound is None else min(len(text), upper_bound)
    lower = max(lower_bound, start - padding)
    upper = min(upper_limit, end + padding)
    preceding = max(text.rfind(marker, lower, start) for marker in ".!?;")
    if preceding >= 0:
        lower = preceding + 1
    following = [
        index for marker in ".!?;" if (index := text.find(marker, end, upper)) >= 0
    ]
    if following:
        upper = min(following)
    return lower, upper


def _association_risk_features(
    text: str,
    expression: TemporalExpression,
    mention: TemporalMention,
    mode: AssociationMode,
) -> tuple[str, ...]:
    _gap, between = _association_gap(text, expression, mention)
    risks: list[str] = []
    if "\n" in between:
        risks.append("layout_newline_crossing")
    if re.search(r"\n[ \t]*\n", between):
        risks.append("layout_break_crossing")
    if mode == "subject_singleton":
        risks.append("cross_field_subject_body")
    if mode == "subject_body_bridge":
        risks.extend(("cross_field_subject_body", "subject_body_bridge_review_only"))
    if mode == "field_near":
        risks.append("field_near_review_only")
        if _gap > _MAX_LOCAL_ASSOCIATION_GAP:
            risks.append("long_association_gap")
        if _FORBIDDEN_ASSOCIATION_PUNCTUATION_RE.search(between):
            risks.append("sentence_punctuation_crossing")
    if mode == "message_singleton":
        risks.append("long_distance_or_sentence_fallback")
    if text and max(expression.end, mention.end) >= int(len(text) * 0.85):
        risks.append("message_tail_context")
    return _ordered_unique(risks)


def _opaque_scope_prefix(chunk_id: str | None, text: str) -> str:
    content_digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    normalized_scope = (
        chunk_id.strip()
        if isinstance(chunk_id, str) and chunk_id.strip()
        else "anonymous"
    )
    material = f"{normalized_scope}\0{content_digest}"
    digest = hashlib.sha256(
        f"pkm-brain/gmail-temporal-leads/v2\0{material}".encode("utf-8")
    ).hexdigest()
    return f"gtl_{digest[:16]}"


def _analysis_snapshot_fingerprint(
    *,
    version: str,
    source_sha256: str,
    anchor: datetime | None,
    chunk_id: str | None,
    scope_bound: bool,
    fact_admitted: bool,
    association_admission_basis: AssociationAdmissionBasis,
    expressions: tuple[TemporalExpression, ...],
    mentions: tuple[TemporalMention, ...],
    leads: tuple[TemporalLead, ...],
    candidate_edge_count: int,
    candidate_edge_count_exact: bool,
    omitted_expression_count: int,
    omitted_mention_count: int,
    graph_truncated: bool,
) -> str:
    """Bind a selector response to one exact, versioned evidence inventory."""

    payload = {
        "schema": "gmail_temporal_analysis_snapshot_v2",
        "analysis_version": version,
        "source_sha256": source_sha256,
        "anchor": anchor.isoformat() if anchor is not None else None,
        "chunk_id_sha256": (
            hashlib.sha256(str(chunk_id).encode("utf-8")).hexdigest()
            if scope_bound
            else None
        ),
        "scope_bound": scope_bound,
        "fact_admitted": fact_admitted,
        "association_admission_basis": association_admission_basis,
        "expressions": [asdict(item) for item in expressions],
        "mentions": [asdict(item) for item in mentions],
        "leads": [asdict(item) for item in leads],
        "candidate_edge_count": candidate_edge_count,
        "candidate_edge_count_exact": candidate_edge_count_exact,
        "omitted_expression_count": omitted_expression_count,
        "omitted_mention_count": omitted_mention_count,
        "retained_edge_count": len(leads),
        "graph_truncated": graph_truncated,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"gta_{hashlib.sha256(encoded).hexdigest()}"


def _resolution_status(options: tuple[str, ...]) -> ResolutionStatus:
    if not options:
        return "unresolved"
    return "ambiguous" if len(options) > 1 else "resolved"


def _span_distance(
    first_start: int, first_end: int, second_start: int, second_end: int
) -> int:
    if first_end <= second_start:
        return second_start - first_end
    if second_end <= first_start:
        return first_start - second_end
    return 0


def _overlaps(start: int, end: int, spans: list[tuple[int, int]]) -> bool:
    return any(
        existing_start < end and start < existing_end
        for existing_start, existing_end in spans
    )


def _ordered_unique(values: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(value for value in values if value))
