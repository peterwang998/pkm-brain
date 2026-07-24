from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from .entities import normalize_entity_name, normalize_entity_type, normalize_mention_kind
from .gmail_sensitive_data import (
    gmail_payload_contains_sensitive_value,
    gmail_sensitive_values,
)
from .gmail_temporal_discovery import gmail_temporal_event_head_is_artifact_object


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
_ISO_DATE_RE = re.compile(r"(?<!\d)(?P<year>\d{4})-(?P<month>\d{2})-(?P<day>\d{2})(?!\d)")
_MONTH_DAY_YEAR_RE = re.compile(
    rf"\b(?P<month>{_MONTH_PATTERN})\.?\s+(?P<day>\d{{1,2}})(?:st|nd|rd|th)?"
    r",?\s+(?P<year>\d{4})\b",
    re.IGNORECASE,
)
_DAY_MONTH_YEAR_RE = re.compile(
    rf"\b(?P<day>\d{{1,2}})(?:st|nd|rd|th)?\s+(?P<month>{_MONTH_PATTERN})\.?,?"
    r"\s+(?P<year>\d{4})\b",
    re.IGNORECASE,
)
_LITERAL_EXACT_TIMESTAMP_RE = re.compile(
    r"(?<!\d)(?P<value>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}"
    r"(?::\d{2}(?:\.\d{1,6})?)?(?:Z|[+-]\d{2}:\d{2}))(?![\w:])",
    re.IGNORECASE,
)
_TIME_OF_DAY_RE = re.compile(
    r"\b\d{1,2}:\d{2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?)?\b",
    re.IGNORECASE,
)
_EVENT_NOUN_RE = re.compile(
    r"\b(?:appointment|booking|briefing|ceremony|conference|consultation|demo|"
    r"dinner|flight|interview|kickoff|launch|meeting|offsite|presentation|"
    r"rehearsal|reservation|seminar|session|stay|rental|trip|visit|workshop)\b",
    re.IGNORECASE,
)
_PLANNED_CUE_RE = re.compile(
    r"\b(?:booked|confirmed|deadline|due|planned|rescheduled|scheduled|targeted)\b"
    r"|\b(?:arrives|begins|departs|ends|starts)\b"
    r"|\b(?:is|was|are|were)\s+set\s+(?:for|to)\b"
    r"|\bwill\s+(?:begin|depart|end|happen|occur|start|take\s+place)\b",
    re.IGNORECASE,
)
_ACTUAL_CUE_RE = re.compile(
    r"\b(?:arrived|began|checked\s+in|checked\s+out|completed|departed|ended|"
    r"happened|occurred|started)\b|\btook\s+place\b|\bwas\s+held\b",
    re.IGNORECASE,
)
_TRAILING_COPULA_RE = re.compile(r"\s+\b(?:are|is|was|were)\b\s*$", re.IGNORECASE)
_DEADLINE_CUE_RE = re.compile(
    r"\b(?:deadline|due|no\s+later\s+than)\b|\bby\s+(?=\w)",
    re.IGNORECASE,
)
_TRAILING_TEMPORAL_CUE_RE = re.compile(
    r"(?:\s+|\b)(?:at|by|for|from|on|through|until)\s*$", re.IGNORECASE
)
_EVENT_PHRASE_STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "at",
    "booked",
    "by",
    "confirmed",
    "for",
    "from",
    "has",
    "is",
    "of",
    "on",
    "planned",
    "rescheduled",
    "scheduled",
    "set",
    "the",
    "to",
    "was",
    "were",
}


@dataclass(frozen=True)
class GmailEventTimeStabilization:
    event_time: dict[str, Any] | None
    entity_mentions: list[dict[str, Any]]
    errors: tuple[str, ...] = ()
    audit: dict[str, Any] | None = None

    @property
    def audit_metadata(self) -> dict[str, Any]:
        return {"gmail_event_time_stabilization": self.audit} if self.audit else {}


@dataclass(frozen=True)
class _DateEvidence:
    iso_date: str
    start: int
    end: int


@dataclass(frozen=True)
class _TemporalSentence:
    text: str
    chunk_id: str
    start: int
    expression_start: int
    expression_end: int
    cue_start: int
    cue_end: int
    kind: str
    source_sensitive_values: tuple[str, ...]


def stabilize_gmail_event_time(
    *,
    source_type: str,
    raw_event_time: Any,
    evidence_text: str,
    entity_mentions: list[dict[str, Any]],
    cited_spans: list[dict[str, Any]],
    chunk_context_by_id: dict[str, dict[str, Any]],
) -> GmailEventTimeStabilization:
    """Repair only literal, unambiguous Gmail event clocks.

    Natural-language bounds from a model are never parsed as authority. Instead,
    the cited expression is re-read deterministically. A time of day survives only
    when the expression itself contains a complete ISO timestamp with an explicit
    UTC offset. Other explicit dates are safely indexed at day precision.
    """

    mentions = [dict(mention) for mention in entity_mentions]
    if source_type != "gmail_thread" or raw_event_time is None:
        return GmailEventTimeStabilization(raw_event_time, mentions)
    if not isinstance(raw_event_time, dict):
        return GmailEventTimeStabilization(raw_event_time, mentions)

    kind = str(raw_event_time.get("kind") or "").strip().lower()
    if kind not in {"actual", "planned"}:
        return GmailEventTimeStabilization(raw_event_time, mentions)
    expression = _optional_text(raw_event_time.get("expression"))
    if not expression or not _literal_in_evidence(expression, evidence_text):
        return _unresolved(
            mentions,
            "gmail event_time requires a literal expression in cited evidence",
        )
    dates = _explicit_dates(expression)
    if not dates:
        return _unresolved(
            mentions,
            "gmail event_time requires an explicit unambiguous date and year",
        )
    if len(dates) > 2:
        return _unresolved(
            mentions,
            "gmail event_time expression contains more than two distinct dates",
        )
    temporal_sentence = _temporal_sentence_for_expression(
        cited_spans, chunk_context_by_id, expression
    )
    if temporal_sentence is None:
        return _unresolved(
            mentions,
            "gmail event_time requires its expression and event in one cited sentence",
        )
    if temporal_sentence.kind != kind:
        return _unresolved(
            mentions,
            "gmail event_time kind is not supported by the cited temporal predicate",
        )
    if len(dates) == 2 and not _explicit_interval_relation(expression, dates):
        return _unresolved(
            mentions,
            "gmail event_time requires an explicit relation between two dates",
        )

    repaired_mentions, identity_mode = _ensure_grounded_primary_event(
        mentions,
        temporal_sentence,
    )
    if identity_mode is None:
        return _unresolved(
            mentions,
            "gmail event_time requires a grounded specific event phrase",
        )

    exact_values = _literal_exact_timestamps(expression)
    use_exact = len(exact_values) == len(dates) and 0 < len(exact_values) <= 2
    bounds = exact_values if use_exact else [item.iso_date for item in dates]
    precision = "exact" if use_exact else "day"
    if len(bounds) == 2 and _bound_sort_key(bounds[1]) <= _bound_sort_key(bounds[0]):
        return _unresolved(
            mentions,
            "gmail event_time end bound is not later than its start bound",
        )

    end_only = bool(
        kind == "planned"
        and _DEADLINE_CUE_RE.search(
            temporal_sentence.text[
                temporal_sentence.cue_start : temporal_sentence.expression_end
            ]
        )
    )
    if len(bounds) == 1 and end_only:
        start_at, end_at = None, bounds[0]
    elif len(bounds) == 1:
        start_at, end_at = bounds[0], None
    elif precision == "day":
        start_at = bounds[0]
        end_at = (date.fromisoformat(bounds[1]) + timedelta(days=1)).isoformat()
    else:
        start_at, end_at = bounds

    return GmailEventTimeStabilization(
        event_time={
            "kind": kind,
            "start_at": start_at,
            "end_at": end_at,
            "precision": precision,
            "expression": expression,
        },
        entity_mentions=repaired_mentions,
        audit={
            "status": "stabilized",
            "basis": "literal_cited_expression",
            "precision": precision,
            "event_identity": identity_mode,
            "time_of_day_discarded": bool(
                precision == "day" and _TIME_OF_DAY_RE.search(expression)
            ),
            "inclusive_end_day_envelope": bool(
                precision == "day" and len(bounds) == 2
            ),
        },
    )


def gmail_temporal_review_reason(candidate: dict[str, Any]) -> str | None:
    """Let uncriticized simple autonomy hold an unresolved Gmail event clock."""

    metadata = candidate.get("metadata")
    if not isinstance(metadata, dict):
        return None
    source_type = str(metadata.get("source_type") or "")
    observed_basis = str(metadata.get("observed_at_basis") or "")
    if source_type != "gmail_thread" and observed_basis not in {
        "gmail_internal_date",
        "gmail_message_time_unresolved",
    }:
        return None
    warnings = metadata.get("temporal_enrichment_warnings")
    if not isinstance(warnings, list):
        return None
    if any(
        isinstance(warning, dict) and warning.get("enrichment") == "event_time"
        for warning in warnings
    ):
        return (
            "Gmail event timing could not be grounded deterministically; review the "
            "event identity and cited date before applying."
        )
    return None


def _unresolved(
    mentions: list[dict[str, Any]], reason: str
) -> GmailEventTimeStabilization:
    return GmailEventTimeStabilization(
        None,
        mentions,
        errors=(reason,),
        audit={"status": "unresolved", "basis": "literal_cited_expression"},
    )


def _explicit_dates(value: str) -> list[_DateEvidence]:
    matches: list[_DateEvidence] = []
    for pattern in (_ISO_DATE_RE, _MONTH_DAY_YEAR_RE, _DAY_MONTH_YEAR_RE):
        for match in pattern.finditer(value):
            try:
                month_text = match.group("month").casefold().rstrip(".")
                month = int(month_text) if month_text.isdigit() else _MONTHS[month_text]
                parsed = date(
                    int(match.group("year")), month, int(match.group("day"))
                )
            except (KeyError, TypeError, ValueError):
                continue
            matches.append(
                _DateEvidence(parsed.isoformat(), match.start(), match.end())
            )
    output: list[_DateEvidence] = []
    occupied: list[tuple[int, int]] = []
    for item in sorted(matches, key=lambda match: (match.start, -(match.end - match.start))):
        if any(start < item.end and item.start < end for start, end in occupied):
            continue
        occupied.append((item.start, item.end))
        if not output or output[-1].iso_date != item.iso_date:
            output.append(item)
    return output


def _explicit_interval_relation(
    expression: str, dates: list[_DateEvidence]
) -> bool:
    if len(dates) != 2:
        return False
    between = expression[dates[0].end : dates[1].start]
    if re.search(r"[;.!?\n]", between):
        return False
    weekday = (
        r"(?:(?:on\s+)?(?:mon(?:day)?|tue(?:sday)?|wed(?:nesday)?|"
        r"thu(?:rsday)?|fri(?:day)?|sat(?:urday)?|sun(?:day)?)\s*,?\s*)?"
    )
    if re.search(
        rf"\b(?:through|to|until)\b\s*{weekday}$", between, re.IGNORECASE
    ):
        return True
    if re.search(
        r"\bcheck[ -]?out\b(?:\s+on)?\s*$", between, re.IGNORECASE
    ):
        return True
    if re.search(
        r"\b(?:arrive|arrives|end|ends|return|returns)\b(?:\s+on)?\s*$",
        between,
        re.IGNORECASE,
    ):
        return True
    return bool(
        re.search(r"\bbetween\b", expression[: dates[0].start], re.IGNORECASE)
        and re.search(r"\band\b\s*$", between, re.IGNORECASE)
    )


def _literal_exact_timestamps(value: str) -> list[str]:
    output: list[str] = []
    for match in _LITERAL_EXACT_TIMESTAMP_RE.finditer(value):
        raw = match.group("value")
        try:
            datetime.fromisoformat(raw.replace("Z", "+00:00").replace("z", "+00:00"))
        except ValueError:
            continue
        if not output or output[-1] != raw:
            output.append(raw)
    return output


def _temporal_sentence_for_expression(
    cited_spans: list[dict[str, Any]],
    chunk_context_by_id: dict[str, dict[str, Any]],
    expression: str,
) -> _TemporalSentence | None:
    for span in cited_spans:
        chunk_id = str(span.get("chunk_id") or "")
        context = chunk_context_by_id.get(chunk_id)
        if context is None:
            continue
        source_text = str(context.get("text") or "")
        try:
            span_start = int(span["start"])
            span_end = int(span["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if span_start < 0 or span_end <= span_start or span_end > len(source_text):
            continue
        cited = source_text[span_start:span_end]
        expression_match = _flexible_literal_match(cited, expression)
        if expression_match is None:
            continue
        sentence_start = max(
            cited.rfind("\n", 0, expression_match.start()),
            cited.rfind(".", 0, expression_match.start()),
            cited.rfind("?", 0, expression_match.start()),
            cited.rfind("!", 0, expression_match.start()),
        ) + 1
        sentence_ends = [
            index
            for marker in ("\n", ".", "?", "!")
            if (index := cited.find(marker, expression_match.end())) >= 0
        ]
        sentence_end = min(sentence_ends) if sentence_ends else len(cited)
        sentence = cited[sentence_start:sentence_end]
        if len(sentence) > 400 or not _EVENT_NOUN_RE.search(sentence):
            continue
        expression_start = expression_match.start() - sentence_start
        expression_end = expression_match.end() - sentence_start
        cues = [
            *[(match, "planned") for match in _PLANNED_CUE_RE.finditer(sentence)],
            *[(match, "actual") for match in _ACTUAL_CUE_RE.finditer(sentence)],
        ]
        for cue, kind in sorted(
            cues, key=lambda item: (item[0].start(), item[0].end()), reverse=True
        ):
            if cue.end() > expression_start or expression_start - cue.end() > 220:
                continue
            association = sentence[cue.end() : expression_start]
            if re.search(r"[;.!?\n]", association) or _explicit_dates(association):
                continue
            if not _cue_targets_expression(sentence, cue, expression_start):
                continue
            nouns = list(_EVENT_NOUN_RE.finditer(sentence, 0, cue.start()))
            if not nouns or cue.start() - nouns[-1].end() > 140:
                continue
            if gmail_temporal_event_head_is_artifact_object(
                sentence,
                event_head_start=nouns[-1].start(),
            ):
                continue
            return _TemporalSentence(
                text=sentence,
                chunk_id=chunk_id,
                start=span_start + sentence_start,
                expression_start=expression_start,
                expression_end=expression_end,
                cue_start=cue.start(),
                cue_end=cue.end(),
                kind=kind,
                source_sensitive_values=gmail_sensitive_values(source_text),
            )
    return None


def _cue_targets_expression(
    sentence: str,
    cue: re.Match[str],
    expression_start: int,
) -> bool:
    cue_text = cue.group(0).strip().casefold()
    if cue_text not in {"booked", "confirmed", "rescheduled"}:
        return True
    association = sentence[cue.end() : expression_start]
    return re.search(r"\b(?:for|to)\s*$", association, re.IGNORECASE) is not None


def _ensure_grounded_primary_event(
    mentions: list[dict[str, Any]],
    temporal_sentence: _TemporalSentence,
) -> tuple[list[dict[str, Any]], str | None]:
    phrase = _grounded_event_phrase(temporal_sentence)
    if phrase is None:
        return mentions, None
    surface, mention_span = phrase
    normalized_surface = normalize_entity_name(surface)
    for index, mention in enumerate(mentions):
        if normalize_entity_name(mention.get("surface")) != normalized_surface:
            continue
        promoted = {
            **mention,
            "entity_type": "event",
            "mention_kind": "named",
            "mention_span": mention_span,
        }
        replaced = [
            {**item, "is_primary": item_index == index}
            for item_index, item in enumerate(mentions)
        ]
        replaced[index] = {**promoted, "is_primary": True}
        retained = (
            normalize_entity_type(mention.get("entity_type")) == "event"
            and normalize_mention_kind(mention.get("mention_kind")) == "named"
        )
        return replaced, (
            "retained_grounded_phrase" if retained else "promoted_grounded_phrase"
        )
    synthesized = {
        "surface": surface,
        "entity_type": "event",
        "mention_kind": "named",
        "is_primary": True,
        "mention_span": mention_span,
        "confidence": None,
    }
    return [
        *[{**mention, "is_primary": False} for mention in mentions],
        synthesized,
    ], "synthesized_grounded_phrase"


def _grounded_event_phrase(
    temporal_sentence: _TemporalSentence,
) -> tuple[str, dict[str, Any]] | None:
    text = temporal_sentence.text
    nouns = list(_EVENT_NOUN_RE.finditer(text, 0, temporal_sentence.cue_start))
    if not nouns:
        return None
    noun = nouns[-1]
    clause_start = _event_clause_start(text, noun.start())
    intervening = text[noun.end() : temporal_sentence.cue_start]
    earlier_cues = [
        *list(_PLANNED_CUE_RE.finditer(intervening)),
        *list(_ACTUAL_CUE_RE.finditer(intervening)),
    ]
    earlier_dates = _explicit_dates(intervening)
    if earlier_cues:
        first_cue_start = noun.end() + min(cue.start() for cue in earlier_cues)
        candidate = text[clause_start:first_cue_start].strip(" \t\r\n,;:-")
        candidate = _TRAILING_COPULA_RE.sub("", candidate).strip(" \t\r\n,;:-")
    elif earlier_dates:
        return None
    else:
        prefix = text[clause_start : temporal_sentence.cue_start].strip(
            " \t\r\n,;:-"
        )
        prefix = _TRAILING_COPULA_RE.sub("", prefix).strip(" \t\r\n,;:-")
        if _specific_event_phrase(prefix):
            candidate = prefix
        else:
            candidate = text[
                clause_start : temporal_sentence.expression_start
            ].strip(" \t\r\n,;:-")
            candidate = _TRAILING_TEMPORAL_CUE_RE.sub("", candidate).strip(
                " \t\r\n,;:-"
            )
    if len(candidate) > 220 or not _specific_event_phrase(candidate):
        return None
    if gmail_payload_contains_sensitive_value(
        {"surface": candidate},
        source_values=temporal_sentence.source_sensitive_values,
    ):
        return None
    candidate_start = text.find(
        candidate,
        clause_start,
        temporal_sentence.cue_start + len(candidate),
    )
    if candidate_start < 0:
        return None
    return candidate, {
        "chunk_id": temporal_sentence.chunk_id,
        "start": temporal_sentence.start + candidate_start,
        "end": temporal_sentence.start + candidate_start + len(candidate),
    }


def _event_clause_start(text: str, noun_start: int) -> int:
    start = max(
        text.rfind(";", 0, noun_start),
        text.rfind(":", 0, noun_start),
    ) + 1
    conjunctions = list(
        re.finditer(r"\b(?:and|but|while)\b", text[start:noun_start], re.IGNORECASE)
    )
    if conjunctions:
        start += conjunctions[-1].end()
    return start


def _specific_event_phrase(value: str) -> bool:
    if not _EVENT_NOUN_RE.search(value):
        return False
    if re.search(r"\b[A-Z]{2,3}\s*\d{1,4}[A-Z]?\b", value):
        return True
    tokens = re.findall(r"[A-Za-z0-9][A-Za-z0-9'-]*", value)
    content = [
        token
        for token in tokens
        if token.casefold() not in _EVENT_PHRASE_STOPWORDS
        and not _EVENT_NOUN_RE.fullmatch(token)
    ]
    return len(content) >= 2 or (
        len(content) == 1 and content[0][:1].isupper()
    )


def _literal_in_evidence(expression: str, evidence: str) -> bool:
    return _flexible_literal_match(str(evidence), expression) is not None


def _flexible_literal_match(value: str, literal: str) -> re.Match[str] | None:
    parts = literal.split()
    if not parts:
        return None
    pattern = r"\s+".join(re.escape(part) for part in parts)
    return re.search(pattern, value, re.IGNORECASE)


def _bound_sort_key(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00").replace("z", "+00:00"))
    if parsed.tzinfo is not None:
        return parsed.astimezone().replace(tzinfo=None)
    return parsed


def _optional_text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None
