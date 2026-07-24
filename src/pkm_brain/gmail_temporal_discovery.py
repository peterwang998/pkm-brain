from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


TemporalRelation = Literal["occurrence", "deadline"]
TemporalKind = Literal["planned", "actual"]
TemporalPrecision = Literal["day", "exact"]


@dataclass(frozen=True)
class TemporalEvidenceSpan:
    """A half-open span into the exact source text passed to discovery."""

    start: int
    end: int
    chunk_id: str | None = None


@dataclass(frozen=True)
class GmailTemporalCandidate:
    """A deterministic temporal lead, not an accepted or routable fact."""

    relation: TemporalRelation
    kind: TemporalKind
    start_at: str | None
    end_at: str | None
    precision: TemporalPrecision
    expression_span: TemporalEvidenceSpan
    cue_span: TemporalEvidenceSpan
    resolution_basis: tuple[str, ...]


@dataclass(frozen=True)
class _DateAtom:
    value: date
    start: int
    end: int
    basis: str
    inferred_year: bool = False


@dataclass(frozen=True)
class _Expression:
    start: int
    end: int
    start_date: date
    end_date: date | None
    start_at: str
    precision: TemporalPrecision
    basis: tuple[str, ...]
    range_end_inclusive: bool = True


@dataclass(frozen=True)
class _Cue:
    relation: TemporalRelation
    kind: TemporalKind
    start: int
    end: int


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
    r"(?:st|nd|rd|th)?\b(?!\s*,?\s*\d+\b)",
    re.IGNORECASE,
)
_DAY_MONTH_RE = re.compile(
    rf"\b(?P<day>\d{{1,2}})(?:st|nd|rd|th)?\s+"
    rf"(?P<month>{_MONTH_PATTERN})\.?\b(?!\s*,?\s*\d+\b)",
    re.IGNORECASE,
)
_RELATIVE_RE = re.compile(
    r"\b(?:today|tomorrow|this\s+coming\s+"
    r"(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday))\b",
    re.IGNORECASE,
)
_WEEKDAY_PREFIX_RE = re.compile(
    r"\b(?P<weekday>monday|tuesday|wednesday|thursday|friday|saturday|sunday)"
    r"\s*,?\s*$",
    re.IGNORECASE,
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
_RANGE_CONNECTOR_RE = re.compile(
    r"^\s*(?:-|–|—|\bto\b|\bthrough\b|\buntil\b)\s*$",
    re.IGNORECASE,
)
_RANGE_YEAR_TAIL_RE = re.compile(
    r"^\s*(?:-|–|—|\bto\b|\bthrough\b|\buntil\b)"
    rf"\s*(?:(?:{_MONTH_PATTERN})\.?\s+)?"
    r"\d{1,2}(?:st|nd|rd|th)?"
    rf"(?:\s+(?:{_MONTH_PATTERN})\.?)?"
    r"(?:\s*,\s*|\s+)(?P<year>\d+)\b",
    re.IGNORECASE,
)
_MALFORMED_MONTH_FIRST_RANGE_RE = re.compile(
    rf"\b(?:{_MONTH_PATTERN})\.?\s+\d{{1,2}}(?:st|nd|rd|th)?\s*"
    rf"(?:-|–|—|to|through|until)\s*(?:(?:{_MONTH_PATTERN})\.?\s+)?"
    r"\d{1,2}(?:st|nd|rd|th)?(?:\s*,\s*|\s+)"
    r"(?!\d{4}\b)\d+\b",
    re.IGNORECASE,
)
_MALFORMED_DAY_FIRST_RANGE_RE = re.compile(
    rf"\b\d{{1,2}}(?:st|nd|rd|th)?(?:\s+(?:{_MONTH_PATTERN})\.?)?\s*"
    rf"(?:-|–|—|to|through|until)\s*\d{{1,2}}(?:st|nd|rd|th)?\s+"
    rf"(?:{_MONTH_PATTERN})\.?(?:\s*,\s*|\s+)"
    r"(?!\d{4}\b)\d+\b",
    re.IGNORECASE,
)

_EVENT_NOUN_RE = re.compile(
    r"\b(?:appointment|booking|briefing|call|ceremony|class|conference|concert|"
    r"consultation|demo|dinner|exam|flight|hearing|interview|kickoff|launch|"
    r"meeting|offsite|party|presentation|rehearsal|reservation|review|seminar|"
    r"session|stay|trip|visit|webinar|workshop)\b",
    re.IGNORECASE,
)
_EVENT_ARTIFACT_RE = re.compile(
    r"\b(?:agenda|attachment|brief|deck|details|dial-in|invite|invitation|link|"
    r"notes|preparation|prep|recording|reminder|room|summary|transcript)\b",
    re.IGNORECASE,
)
_EVENT_ARTIFACT_OBJECT_LINK_RE = re.compile(
    r"[ \t]+(?:of|from)[ \t]+"
    r"(?:(?:a|an|the|this|that|our|your)[ \t]+)?"
    r"(?:[A-Za-z0-9][A-Za-z0-9'\N{RIGHT SINGLE QUOTATION MARK}.-]*[ \t]+){0,4}\Z",
    re.IGNORECASE,
)
_EVENT_BOUNDARY_LABEL_RE = re.compile(
    r"\b(?:arrival|completion)\b",
    re.IGNORECASE,
)
_PLANNED_CUE_RE = re.compile(
    r"\b(?:(?:is|are|was|were)\s+(?:now\s+)?)?scheduled\s+(?:for|on)\b"
    r"|\b(?:(?:is|are|was|were|has\s+been|have\s+been)\s+)?"
    r"rescheduled\s+(?:for|to)\b"
    r"|\b(?:(?:is|are|was|were)\s+)?(?:booked|confirmed)\s+for\b"
    r"|\b(?:starts?|begins?|departs?)(?:\s+(?:on|at))?\b"
    r"|\bwill\s+(?:start|begin|depart|occur|happen|take\s+place|"
    r"be\s+held)(?:\s+(?:on|at))?\b"
    r"|\btakes?\s+place(?:\s+on)?\b",
    re.IGNORECASE,
)
_ACTUAL_CUE_RE = re.compile(
    r"\b(?:occurred|happened|started|began|departed)"
    r"(?:\s+on|\s+at)?\b|\btook\s+place(?:\s+on)?\b|\bwas\s+held\s+on\b",
    re.IGNORECASE,
)
_DEADLINE_CUE_RE = re.compile(
    r"\b(?:deadline|due\s+date)\s*(?:is|:)?"
    r"|\b(?:is|are|was|were)\s+due(?:\s+(?:on|by))?\b"
    r"|\bdue(?:\s+(?:on|by))?\b(?!\s+to\b)"
    r"|\bno\s+later\s+than\b"
    r"|\b(?:applications?|registration|submissions?)\s+(?:close|closes)\b",
    re.IGNORECASE,
)
GMAIL_TEMPORAL_ACTION_VERBS = (
    "accept",
    "apply",
    "approve",
    "book",
    "complete",
    "confirm",
    "decline",
    "file",
    "finish",
    "pay",
    "provide",
    "register",
    "renew",
    "reply",
    "respond",
    "rsvp",
    "send",
    "sign",
    "submit",
    "upload",
    "verify",
)
_ACTION_BEFORE_BY_RE = re.compile(
    rf"\b(?:{'|'.join(GMAIL_TEMPORAL_ACTION_VERBS)})\b",
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
_INTAKE_THIRD_PERSON_OPENING_RES = (
    re.compile(
        rf"\A[ \t]*(?:(?:our|the|your)[ \t]+)?"
        rf"(?P<subject>{_INTAKE_SINGULAR_SUBJECT_PATTERN})"
        r"[ \t]+(?P<cue>opens)(?:[ \t]+(?:at|on))?[ \t]*\Z",
        re.IGNORECASE,
    ),
    re.compile(
        rf"\A[ \t]*(?:(?:our|the|your)[ \t]+)?"
        rf"(?P<subject>{_INTAKE_PLURAL_SUBJECT_PATTERN})"
        r"[ \t]+(?P<cue>open)(?:[ \t]+(?:at|on))?[ \t]*\Z",
        re.IGNORECASE,
    ),
)
_CLAUSE_END_AFTER_EXPRESSION_RE = re.compile(
    r"\A[ \t]*(?:[)\]}\"'\N{RIGHT DOUBLE QUOTATION MARK}"
    r"\N{RIGHT SINGLE QUOTATION MARK}][ \t]*)*(?:[.!?;]|\r?\n|\Z)"
)
_OPENING_TO_CLOSING_CONNECTOR_RE = re.compile(
    r"\A[ \t]*(?:,[ \t]*)?(?:and|then)[ \t]+closes"
    r"(?:[ \t]+(?:at|on))?[ \t]*\Z",
    re.IGNORECASE,
)

_ZONE_PATTERN = (
    r"(?:Z|UTC|GMT|(?:UTC|GMT)\s*[+-]\d{1,2}(?::?\d{2})?|"
    r"[+-]\d{2}:?\d{2}|PST|PDT|MST|MDT|CST|CDT|EST|EDT|"
    r"(?:Africa|America|Antarctica|Arctic|Asia|Atlantic|Australia|Europe|"
    r"Indian|Pacific)/[A-Za-z_+-]+(?:/[A-Za-z_+-]+)?)"
)
_CLOCK_AFTER_RE = re.compile(
    rf"^(?P<lead>\s*(?:(?:,?\s*(?:at|@)\s+)|T|,\s*|\s+))"
    rf"(?P<hour>[01]?\d|2[0-3])(?::(?P<minute>[0-5]\d)"
    rf"(?::(?P<second>[0-5]\d)(?:\.(?P<fraction>\d{{1,6}}))?)?)?\s*"
    rf"(?P<ampm>a\.?m\.?|p\.?m\.?)?\s*(?P<zone>{_ZONE_PATTERN})\b",
    re.IGNORECASE,
)
_LOCAL_CLOCK_AFTER_RE = re.compile(
    r"^\s*(?:(?:,?\s*(?:at|@)\s+)|T|,\s*|\s+)"
    r"(?:[01]?\d|2[0-3])(?::[0-5]\d(?::[0-5]\d(?:\.\d{1,6})?)?)"
    r"\s*(?:a\.?m\.?|p\.?m\.?)?\b",
    re.IGNORECASE,
)

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
_MAX_YEAR_INFERENCE_DISTANCE = 183


def discover_gmail_temporal_candidates(
    *,
    text: str,
    message_internal_at: str | datetime,
    chunk_id: str | None = None,
) -> tuple[GmailTemporalCandidate, ...]:
    """Discover high-confidence temporal leads without routing or persistence.

    ``message_internal_at`` must be an aware trusted provider time (ISO-8601 or
    Gmail epoch milliseconds). It anchors only missing-year and relative phrases.
    The returned source offsets are exact, half-open offsets into ``text``.
    """

    anchor = _aware_internal_time(message_internal_at)
    if not text or anchor is None:
        return ()
    expressions = _discover_expressions(text, anchor.date())
    output: list[GmailTemporalCandidate] = []
    for expression in expressions:
        cue = _cue_for_expression(text, expression, expressions)
        if cue is None or (cue.relation == "deadline" and expression.end_date):
            continue
        if cue.relation == "deadline":
            start_at = None
            end_at = expression.start_at
        else:
            start_at = expression.start_at
            end_at = (
                (
                    expression.end_date
                    + timedelta(days=1 if expression.range_end_inclusive else 0)
                ).isoformat()
                if expression.end_date
                else None
            )
        output.append(
            GmailTemporalCandidate(
                relation=cue.relation,
                kind=cue.kind,
                start_at=start_at,
                end_at=end_at,
                precision=expression.precision,
                expression_span=TemporalEvidenceSpan(
                    expression.start, expression.end, chunk_id
                ),
                cue_span=TemporalEvidenceSpan(cue.start, cue.end, chunk_id),
                resolution_basis=expression.basis,
            )
        )
    return tuple(
        sorted(
            _deduplicate(output),
            key=lambda item: (item.expression_span.start, item.cue_span.start),
        )
    )


def _discover_expressions(text: str, anchor: date) -> list[_Expression]:
    ranges = _shared_ranges(text, anchor)
    atoms = _date_atoms(text, anchor)
    ranges.extend(_paired_ranges(text, atoms, ranges))
    occupied = [(item.start, item.end) for item in ranges]
    expressions = list(ranges)
    for atom in atoms:
        if _overlaps(atom.start, atom.end, occupied):
            continue
        end, exact, basis = _clock_for_date(text, atom)
        expressions.append(
            _Expression(
                start=atom.start,
                end=end,
                start_date=atom.value,
                end_date=None,
                start_at=exact or atom.value.isoformat(),
                precision="exact" if exact else "day",
                basis=(atom.basis, *basis),
            )
        )
    return sorted(expressions, key=lambda item: (item.start, -(item.end - item.start)))


def _date_atoms(text: str, anchor: date) -> list[_DateAtom]:
    matches: list[_DateAtom] = []
    malformed_ranges = [
        (match.start(), match.end())
        for pattern in (
            _MALFORMED_MONTH_FIRST_RANGE_RE,
            _MALFORMED_DAY_FIRST_RANGE_RE,
        )
        for match in pattern.finditer(text)
    ]
    for pattern, basis in (
        (_ISO_DATE_RE, "explicit_iso_date"),
        (_MONTH_DAY_YEAR_RE, "explicit_month_day_year"),
        (_DAY_MONTH_YEAR_RE, "explicit_day_month_year"),
    ):
        for match in pattern.finditer(text):
            if _overlaps(match.start(), match.end(), malformed_ranges):
                continue
            parsed = _date_from_match(match)
            if parsed and _weekday_prefix_agrees(text, match.start(), parsed):
                matches.append(_DateAtom(parsed, match.start(), match.end(), basis))
    for pattern, basis in (
        (_MONTH_DAY_RE, "month_day_year_inferred_from_message_internal_at"),
        (_DAY_MONTH_RE, "day_month_year_inferred_from_message_internal_at"),
    ):
        for match in pattern.finditer(text):
            if _overlaps(match.start(), match.end(), malformed_ranges):
                continue
            if _followed_by_malformed_range_year(text, match.end()):
                continue
            inferred = _infer_year(
                _month_number(match.group("month")), int(match.group("day")), anchor
            )
            if inferred and _weekday_prefix_agrees(text, match.start(), inferred):
                matches.append(
                    _DateAtom(
                        inferred, match.start(), match.end(), basis, inferred_year=True
                    )
                )
    for match in _RELATIVE_RE.finditer(text):
        raw = re.sub(r"\s+", " ", match.group(0).casefold())
        if raw == "today":
            resolved = anchor
            basis = "relative_today_from_message_internal_at"
        elif raw == "tomorrow":
            resolved = anchor + timedelta(days=1)
            basis = "relative_tomorrow_from_message_internal_at"
        else:
            weekday = _WEEKDAYS[raw.removeprefix("this coming ")]
            delta = (weekday - anchor.weekday()) % 7 or 7
            resolved = anchor + timedelta(days=delta)
            basis = "relative_this_coming_weekday_from_message_internal_at"
        matches.append(_DateAtom(resolved, match.start(), match.end(), basis))

    output: list[_DateAtom] = []
    occupied: list[tuple[int, int]] = []
    for item in sorted(
        matches, key=lambda value: (value.start, -(value.end - value.start))
    ):
        if _overlaps(item.start, item.end, occupied):
            continue
        output.append(item)
        occupied.append((item.start, item.end))
    return output


def _shared_ranges(text: str, anchor: date) -> list[_Expression]:
    output: list[_Expression] = []
    occupied: list[tuple[int, int]] = []
    for pattern in (_SHARED_MONTH_RANGE_RE, _SHARED_DAY_FIRST_RANGE_RE):
        for match in pattern.finditer(text):
            if _overlaps(match.start(), match.end(), occupied):
                continue
            month = _month_number(match.group("month"))
            year_text = match.group("year")
            if year_text:
                year = int(year_text)
                basis = "textual_range_with_explicit_year"
            else:
                inferred = _infer_year(month, int(match.group("first")), anchor)
                if inferred is None:
                    continue
                year = inferred.year
                basis = "textual_range_year_inferred_from_message_internal_at"
            try:
                first = date(year, month, int(match.group("first")))
                last = date(year, month, int(match.group("last")))
            except ValueError:
                continue
            if last <= first or (last - first).days > 366:
                continue
            output.append(
                _Expression(
                    match.start(),
                    match.end(),
                    first,
                    last,
                    first.isoformat(),
                    "day",
                    (
                        basis,
                        (
                            "exclusive_until_date_range"
                            if match.group("connector").casefold() == "until"
                            else "inclusive_textual_date_range"
                        ),
                    ),
                    match.group("connector").casefold() != "until",
                )
            )
            occupied.append((match.start(), match.end()))
    return output


def _paired_ranges(
    text: str, atoms: list[_DateAtom], existing: list[_Expression]
) -> list[_Expression]:
    occupied = [(item.start, item.end) for item in existing]
    output: list[_Expression] = []
    for first, last in zip(atoms, atoms[1:]):
        if _overlaps(first.start, last.end, occupied):
            continue
        if not _RANGE_CONNECTOR_RE.fullmatch(text[first.end : last.start]):
            continue
        start_date, end_date = _cohere_range_years(first, last)
        if start_date is None or end_date is None:
            continue
        days = (end_date - start_date).days
        if days <= 0 or days > 366:
            continue
        connector = text[first.end : last.start].strip().casefold()
        inclusive_end = connector != "until"
        basis = [
            first.basis,
            last.basis,
            (
                "inclusive_textual_date_range"
                if inclusive_end
                else "exclusive_until_date_range"
            ),
        ]
        if first.inferred_year != last.inferred_year:
            basis.append("range_year_cohered_to_explicit_endpoint")
        output.append(
            _Expression(
                first.start,
                last.end,
                start_date,
                end_date,
                start_date.isoformat(),
                "day",
                tuple(dict.fromkeys(basis)),
                inclusive_end,
            )
        )
    return output


def _cohere_range_years(
    first: _DateAtom, last: _DateAtom
) -> tuple[date | None, date | None]:
    start_date = first.value
    end_date = last.value
    try:
        if first.inferred_year and not last.inferred_year:
            start_date = date(last.value.year, first.value.month, first.value.day)
            if start_date > end_date:
                start_date = date(
                    last.value.year - 1, first.value.month, first.value.day
                )
        elif last.inferred_year and not first.inferred_year:
            end_date = date(first.value.year, last.value.month, last.value.day)
            if end_date <= start_date:
                end_date = date(first.value.year + 1, last.value.month, last.value.day)
    except ValueError:
        return None, None
    return start_date, end_date


def _clock_for_date(
    text: str, atom: _DateAtom
) -> tuple[int, str | None, tuple[str, ...]]:
    tail = text[atom.end : atom.end + 80]
    match = _CLOCK_AFTER_RE.match(tail)
    if match is None:
        if _LOCAL_CLOCK_AFTER_RE.match(tail):
            return atom.end, None, ("clock_time_discarded_missing_timezone",)
        return atom.end, None, ()
    minute_text = match.group("minute")
    ampm = match.group("ampm")
    if minute_text is None and ampm is None:
        return atom.end, None, ()
    hour = int(match.group("hour"))
    minute = int(minute_text or 0)
    second = int(match.group("second") or 0)
    fraction = match.group("fraction") or ""
    microsecond = int(fraction.ljust(6, "0")) if fraction else 0
    if ampm is None and hour <= 12 and len(match.group("hour")) == 1:
        return atom.end, None, ("clock_time_discarded_ambiguous_meridiem",)
    if ampm:
        if hour < 1 or hour > 12:
            return atom.end, None, ()
        marker = ampm.replace(".", "").casefold()
        hour = hour % 12 + (12 if marker == "pm" else 0)
    tzinfo, zone_basis = _timezone_for_text(match.group("zone"))
    if tzinfo is None:
        return atom.end, None, ()
    if isinstance(tzinfo, ZoneInfo) and not _iana_local_time_is_unambiguous(
        atom.value, hour, minute, second, microsecond, tzinfo
    ):
        return (
            atom.end,
            None,
            ("clock_time_discarded_invalid_or_ambiguous_timezone",),
        )
    resolved = datetime.combine(
        atom.value,
        time(hour, minute, second, microsecond),
        tzinfo=tzinfo,
    )
    return (
        atom.end + match.end(),
        resolved.isoformat(timespec="microseconds" if microsecond else "seconds"),
        ("explicit_clock_time", zone_basis),
    )


def _timezone_for_text(value: str) -> tuple[timezone | ZoneInfo | None, str]:
    compact = value.strip().upper().replace(" ", "")
    if compact in {"Z", "UTC", "GMT"}:
        return timezone.utc, "explicit_utc_designator"
    if compact in _NORTH_AMERICAN_ABBREVIATION_OFFSETS:
        return (
            timezone(timedelta(hours=_NORTH_AMERICAN_ABBREVIATION_OFFSETS[compact])),
            "fixed_north_american_timezone_abbreviation",
        )
    numeric = re.fullmatch(r"(?:(?:UTC|GMT))?([+-])(\d{1,2})(?::?(\d{2}))?", compact)
    if numeric:
        hours = int(numeric.group(2))
        minutes = int(numeric.group(3) or 0)
        if hours > 14 or minutes > 59 or (hours == 14 and minutes):
            return None, ""
        delta = timedelta(hours=hours, minutes=minutes)
        if numeric.group(1) == "-":
            delta = -delta
        return timezone(delta), "explicit_numeric_utc_offset"
    try:
        return ZoneInfo(value), "explicit_iana_timezone"
    except (ValueError, ZoneInfoNotFoundError):
        return None, ""


def _cue_for_expression(
    text: str,
    expression: _Expression,
    expressions: list[_Expression],
) -> _Cue | None:
    clause_start = (
        max(
            text.rfind("\n", 0, expression.start),
            text.rfind(".", 0, expression.start),
            text.rfind("?", 0, expression.start),
            text.rfind("!", 0, expression.start),
            text.rfind(";", 0, expression.start),
        )
        + 1
    )
    prefix = text[clause_start : expression.start]
    if len(prefix) > 280:
        prefix = prefix[-280:]
        clause_start = expression.start - len(prefix)

    cues: list[_Cue] = []
    direct_action_by_cues: list[_Cue] = []
    intake_opening = next(
        (
            match
            for pattern in _INTAKE_THIRD_PERSON_OPENING_RES
            if (match := pattern.fullmatch(prefix)) is not None
        ),
        None,
    )
    if intake_opening is not None and _bounded_intake_opening_tail(
        text,
        expression=expression,
        expressions=expressions,
    ):
        cue_start, cue_end = intake_opening.span("cue")
        cues.append(
            _Cue(
                "occurrence",
                "planned",
                clause_start + cue_start,
                clause_start + cue_end,
            )
        )
    for pattern, kind in ((_PLANNED_CUE_RE, "planned"), (_ACTUAL_CUE_RE, "actual")):
        for match in pattern.finditer(prefix):
            noun_prefix = prefix[: match.start()]
            nouns = list(_EVENT_NOUN_RE.finditer(noun_prefix))
            if not nouns or match.start() - nouns[-1].end() > 180:
                continue
            if gmail_temporal_event_head_is_artifact_object(
                noun_prefix,
                event_head_start=nouns[-1].start(),
            ):
                continue
            event_subject_tail = prefix[nouns[-1].end() : match.start()]
            if _EVENT_ARTIFACT_RE.search(
                event_subject_tail
            ) or _EVENT_BOUNDARY_LABEL_RE.search(event_subject_tail):
                continue
            if not _occurrence_gap_targets_expression(prefix[match.end() :]):
                continue
            cues.append(
                _Cue(
                    "occurrence",
                    kind,
                    clause_start + match.start(),
                    clause_start + match.end(),
                )
            )
    for match in _DEADLINE_CUE_RE.finditer(prefix):
        if _deadline_gap_targets_expression(prefix[match.end() :]):
            cues.append(
                _Cue(
                    "deadline",
                    "planned",
                    clause_start + match.start(),
                    clause_start + match.end(),
                )
            )
    for match in re.finditer(r"\bby\b", prefix, re.IGNORECASE):
        action_window = prefix[max(0, match.start() - 90) : match.start()]
        actions = list(_ACTION_BEFORE_BY_RE.finditer(action_window))
        action_tail = action_window[actions[-1].end() :] if actions else ""
        crosses_predicate = bool(
            re.search(r"[,;]\s*(?:and|but)\b", action_tail, re.IGNORECASE)
            or re.search(
                r"\b(?:is|are|was|were|has|have|had|will)\b",
                action_tail,
                re.IGNORECASE,
            )
        )
        if (
            actions
            and not crosses_predicate
            and _action_by_gap_targets_expression(prefix[match.end() :])
        ):
            cue = _Cue(
                "deadline",
                "planned",
                clause_start + match.start(),
                clause_start + match.end(),
            )
            cues.append(cue)
            direct_action_by_cues.append(cue)
    if direct_action_by_cues:
        # ``submit ... by the deadline: DATE`` contains both a generic deadline
        # noun and the more informative action connector.  Preserve the action
        # cue so the lead layer can bind the date to ``submit``.  This priority
        # is safe because the connector was just proven to target this exact
        # expression rather than an intervening agent (``by Alice``).
        return max(direct_action_by_cues, key=lambda item: item.end)
    return max(cues, key=lambda item: item.end, default=None)


def gmail_temporal_event_head_is_artifact_object(
    text: str,
    *,
    event_head_start: int,
) -> bool:
    """Return whether an event head is the bounded object of an artifact noun."""

    if (
        not isinstance(text, str)
        or isinstance(event_head_start, bool)
        or not isinstance(event_head_start, int)
        or not 0 <= event_head_start <= len(text)
    ):
        return False
    prefix = text[:event_head_start]
    artifacts = list(_EVENT_ARTIFACT_RE.finditer(prefix))
    if not artifacts:
        return False
    return (
        _EVENT_ARTIFACT_OBJECT_LINK_RE.fullmatch(prefix[artifacts[-1].end() :])
        is not None
    )


def _bounded_intake_opening_tail(
    text: str,
    *,
    expression: _Expression,
    expressions: list[_Expression],
) -> bool:
    """Admit a terminal opening or one exact coordinated closing endpoint."""

    if _CLAUSE_END_AFTER_EXPRESSION_RE.match(text[expression.end :]) is not None:
        return True
    following = min(
        (item for item in expressions if item.start >= expression.end),
        key=lambda item: (item.start, item.end),
        default=None,
    )
    return bool(
        following is not None
        and _OPENING_TO_CLOSING_CONNECTOR_RE.fullmatch(
            text[expression.end : following.start]
        )
        is not None
        and _CLAUSE_END_AFTER_EXPRESSION_RE.match(text[following.end :]) is not None
    )


def _clean_cue_gap(value: str) -> bool:
    if len(value) > 180 or re.search(r"[;.!?\n]", value):
        return False
    return not re.search(
        rf"\b(?:{_MONTH_PATTERN})\.?\s+\d{{1,2}}|\b\d{{4}}-\d{{2}}-\d{{2}}\b|"
        r"\b(?:today|tomorrow)\b",
        value,
        re.IGNORECASE,
    )


def _action_by_gap_targets_expression(value: str) -> bool:
    """Accept only bounded date connectors after an action's ``by`` cue.

    The former permissive gap accepted an arbitrary noun phrase, so an
    agentive phrase such as ``send the report prepared by Alice on DATE`` was
    misread as a submission deadline.  Keep ordinary direct deadlines and a
    small set of explicit business-day/date labels while rejecting names and
    free prose between ``by`` and the temporal expression.
    """

    if not _clean_cue_gap(value):
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


def _occurrence_gap_targets_expression(value: str) -> bool:
    if not _clean_cue_gap(value):
        return False
    return not re.search(
        r",\s*(?:and|but|where|which|whose)\b"
        r"|\b(?:is|are|was|were|has|have|had|will)\b"
        r"|^\s+[A-Za-z][A-Za-z'-]*ing\b",
        value,
        re.IGNORECASE,
    )


def _deadline_gap_targets_expression(value: str) -> bool:
    if not _clean_cue_gap(value):
        return False
    gap = value.strip(" \t\r\n,;:-")
    if not gap:
        return True
    if re.fullmatch(
        r"(?:is|are|was|were|has\s+been|have\s+been)?\s*"
        r"(?:extended|moved|pushed|rescheduled|set)\s+(?:for|to|until)",
        gap,
        re.IGNORECASE,
    ):
        return True
    return (
        re.fullmatch(
            r"for\s+[A-Za-z0-9][A-Za-z0-9 '\u2019/-]{0,70}\s+"
            r"(?:is|are|was|were|will\s+be|has\s+been|have\s+been)",
            gap,
            re.IGNORECASE,
        )
        is not None
    )


def _iana_local_time_is_unambiguous(
    day: date,
    hour: int,
    minute: int,
    second: int,
    microsecond: int,
    zone: ZoneInfo,
) -> bool:
    naive = datetime.combine(day, time(hour, minute, second, microsecond))
    valid_offsets: set[timedelta | None] = set()
    for fold in (0, 1):
        localized = naive.replace(tzinfo=zone, fold=fold)
        round_trip = localized.astimezone(timezone.utc).astimezone(zone)
        if round_trip.replace(tzinfo=None) == naive:
            valid_offsets.add(localized.utcoffset())
    return len(valid_offsets) == 1


def _weekday_prefix_agrees(text: str, start: int, value: date) -> bool:
    prefix = text[max(0, start - 24) : start]
    match = _WEEKDAY_PREFIX_RE.search(prefix)
    if match is None:
        return True
    return _WEEKDAYS[match.group("weekday").casefold()] == value.weekday()


def _followed_by_malformed_range_year(text: str, end: int) -> bool:
    match = _RANGE_YEAR_TAIL_RE.match(text[end : end + 80])
    return match is not None and len(match.group("year")) != 4


def _aware_internal_time(value: str | datetime) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
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
    raw = value.casefold().rstrip(".")
    return int(raw) if raw.isdigit() else _MONTHS[raw]


def _infer_year(month: int, day: int, anchor: date) -> date | None:
    choices: list[tuple[int, date]] = []
    for year in (anchor.year - 1, anchor.year, anchor.year + 1):
        try:
            candidate = date(year, month, day)
        except ValueError:
            continue
        distance = abs((candidate - anchor).days)
        if distance <= _MAX_YEAR_INFERENCE_DISTANCE:
            choices.append((distance, candidate))
    if not choices:
        return None
    choices.sort(key=lambda item: (item[0], item[1]))
    if len(choices) > 1 and choices[0][0] == choices[1][0]:
        return None
    return choices[0][1]


def _overlaps(start: int, end: int, spans: list[tuple[int, int]]) -> bool:
    return any(
        existing_start < end and start < existing_end
        for existing_start, existing_end in spans
    )


def _deduplicate(
    candidates: list[GmailTemporalCandidate],
) -> list[GmailTemporalCandidate]:
    output: list[GmailTemporalCandidate] = []
    seen: set[tuple[object, ...]] = set()
    for candidate in candidates:
        key = (
            candidate.relation,
            candidate.kind,
            candidate.start_at,
            candidate.end_at,
            candidate.expression_span.start,
            candidate.expression_span.end,
        )
        if key not in seen:
            seen.add(key)
            output.append(candidate)
    return output
