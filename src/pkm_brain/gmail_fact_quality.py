from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any


_REQUEST_EVIDENCE = re.compile(
    r"\b(?:can|could|would|will)\s+you\b"
    r"|\b(?:please|kindly)\s+(?:add|arrange|book|bring|call|complete|confirm|"
    r"contact|ensure|keep|let|make|pay|prepare|provide|reply|review|schedule|"
    r"send|set|share|sign|submit|update|use)\b"
    r"|\b(?:request(?:ed|ing|s)?|ask(?:ed|ing|s)?)\b",
    re.IGNORECASE,
)
_ACCEPTED_COMMITMENT_EVIDENCE = re.compile(
    r"\b(?:i|we|he|she|they)\s+(?:agree(?:d)?|commit(?:ted)?|promise(?:d)?|"
    r"shall|will)\b"
    r"|\b(?:i|we|he|she|they)['’]ll\b"
    r"|\b(?:agreed|committed|promised)\s+to\b"
    r"|\bconsider\s+it\s+done\b",
    re.IGNORECASE,
)
_REQUEST_REPORTING_STATEMENT = re.compile(
    r"\b(?:ask(?:ed|s)?|request(?:ed|s)?)\b", re.IGNORECASE
)
_CONDITIONAL_SCHEDULING_INSTRUCTION = re.compile(
    r"\b(?:if\s+(?:you\s+(?:are\s+interested\s+in|need\s+to|want\s+to|"
    r"wish\s+to|would\s+like\s+to)|you['’]d\s+like\s+to)|"
    r"should\s+you\s+(?:need|want|wish)\s+to)"
    r"[^.!?\n]{0,180}\b(?:arrange|book|call|contact|meet|reschedule|schedule|"
    r"set\s+up)\b",
    re.IGNORECASE,
)
_CONDITIONAL_DECLARATIVE_STATEMENT = re.compile(
    r"\bif\b[^.!?\n]{1,180}\b(?:can|may|should|will|would)\b",
    re.IGNORECASE,
)
_UNRESOLVED_QUESTION_STATEMENT = re.compile(
    r"\b(?:not\s+known|open\s+question|remains\s+(?:open|unclear|unknown)|"
    r"unclear|unknown|unresolved|whether)\b",
    re.IGNORECASE,
)
_LEGAL_CONTEXT = re.compile(
    r"\b(?:according\s+to|in\s+accordance\s+with|pursuant\s+to|required\s+by|"
    r"under)\s+(?P<context>[^.;\n]{1,80}?\b(?:law|laws|regulation|regulations|"
    r"rule|rules|statute|statutes))\b",
    re.IGNORECASE,
)
_EVIDENCE_ONE_OFF_CONTEXT = re.compile(
    r"\b(?:for|during|in|as\s+part\s+of)\s+(?:this|that|the|your)\s+"
    r"(?:appointment|booking|event|flight|interview|meeting|order|rental|"
    r"reservation|stay|trip|visit)\b"
    r"|\b(?:this|that|your)\s+(?:appointment|booking|event|flight|interview|"
    r"meeting|order|rental|reservation|stay|trip|visit)\b",
    re.IGNORECASE,
)
_STATEMENT_CONTEXT_QUALIFIER = re.compile(
    r"\b(?:this|that|specific|the|your)\s+(?:appointment|booking|event|flight|"
    r"interview|meeting|order|rental|reservation|stay|trip|visit)\b"
    r"|\b(?:appointment|booking|event|flight|interview|meeting|order|rental|"
    r"reservation|stay|trip|visit)\b",
    re.IGNORECASE,
)
_GENERIC_RULE_STATEMENT = re.compile(
    r"\b(?:all\s+)?(?:applicants?|attendees?|candidates?|companies|customers?|"
    r"employees?|guests?|hosts?|hotels?|passengers?|travelers?|users?|visitors?)\b"
    r"[^.!?\n]{0,120}\b(?:are\s+allowed|are\s+required|can(?:not)?|cannot|"
    r"have\s+to|may|must|need\s+to|should)\b"
    r"|\b(?:always|never|universally)\b"
    r"|\b(?:allows?|mandates?|permits?|prohibits?|requires?)\b",
    re.IGNORECASE,
)
_SOURCE_SCOPED_NORMATIVE_RULE = re.compile(
    r"\b(?:must(?:\s+not)?|may\s+not|cannot|can['’]?t|never|strictly\s+"
    r"prohibited|required\s+to|prohibited\s+from)\b",
    re.IGNORECASE,
)
_SOURCE_AUTHORITY_QUALIFIER = re.compile(
    r"\b(?:according\s+to|per|pursuant\s+to|under)\b"
    r"|\b(?:airline|carrier|jurisdiction|law|notice|operator|policy|provider|"
    r"regulation)\b"
    r"|\b(?:for|on)\s+(?:this|that|the|your|[A-Z][A-Za-z0-9&.'-]+)\s+"
    r"(?:airline|carrier|flight|service|trip)\b",
)
_ACCESS_DETAIL = re.compile(
    r"\b(?:access|booking|confirmation|door|entry|gate|reservation|security)\s+"
    r"(?:code|id|identifier|number|reference)\b"
    r"|\b(?:booking\s+reference|confirmation\s+code|dial[ -]?in|meeting\s+link|"
    r"passcode|password|pin|pnr|record\s+locator|ticket\s+number|token|"
    r"wi[ -]?fi(?:\s+password)?|zoom\s+link)\b"
    r"|\b(?:gate|room|seat|terminal)\s+(?:assignment|number)\b",
    re.IGNORECASE,
)
_CONCRETE_ACCESS_VALUE = re.compile(
    r"\b(?:code|id|identifier|locator|number|passcode|password|pin|reference|"
    r"token)\b\s*(?:is|was|:|=)\s*\S+",
    re.IGNORECASE,
)
_CAPABILITY_PREDICATE = re.compile(
    r"\b(?:allows?|can|enables?|generates?|manages?|offers?|provides?|supports?)\b",
    re.IGNORECASE,
)
_ROUTINE_EVENT_ATTRIBUTE = re.compile(
    r"\b(?:booked\s+by|booker|booking\s+contact|guest\s+count|number\s+of\s+"
    r"guests|primary\s+guest)\b"
    r"|\b(?:one|two|three|four|five|six|seven|eight|nine|ten|\d+)\s+"
    r"(?:adults?|children|guests?|infants?)\b"
    r"|\b(?:amenit(?:y|ies)|bed\s+type|breakfast\s+included|cancellation\s+"
    r"policy|meal\s+preference|parking|refund\s+policy|room\s+type|seat\s+"
    r"assignment|sofa\s+bed)\b"
    r"|\b(?:arrival|check[ -]?in|entry)\s+instructions?\b"
    r"|\b(?:payment|payout|price|total)\s+(?:amount|was|is|of)\b",
    re.IGNORECASE,
)
_ONE_OFF_EVENT_NOUN = re.compile(
    r"\b(?:appointment|booking|event|flight|interview|meeting|rental|reservation|"
    r"stay|trip|visit)\b",
    re.IGNORECASE,
)
_EXPLICIT_TIME = re.compile(
    r"\b(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
    r"jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|"
    r"dec(?:ember)?)\b"
    r"|\b\d{4}-\d{2}(?:-\d{2})?\b"
    r"|\b\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?\b"
    r"|\b\d{1,2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?)\b",
    re.IGNORECASE,
)
_SCHEDULE_PREDICATE = re.compile(
    r"\b(?:arriv(?:al|e|es|ing)|begin(?:s)?|check[ -]?(?:in|out)|depart(?:s|ure)?|"
    r"due|end(?:s)?|occur(?:s|red)?|rescheduled|scheduled|start(?:s)?|"
    r"takes?\s+place)\b",
    re.IGNORECASE,
)
_EVENT_IDENTITY = re.compile(
    r"\b(?:appointment|booking|event|flight|interview|meeting|rental|reservation|"
    r"stay|trip|visit)\b[^.!?\n]{0,80}\b(?:booked|cancelled|canceled|confirmed|"
    r"rescheduled|scheduled)\b"
    r"|\b(?:booked|cancelled|canceled|confirmed|rescheduled|scheduled)\b"
    r"[^.!?\n]{0,80}\b(?:appointment|booking|event|flight|interview|meeting|"
    r"rental|reservation|stay|trip|visit)\b",
    re.IGNORECASE,
)
_OBLIGATION = re.compile(
    r"\b(?:agreed|asked|committed|deadline|due|has\s+to|must|needs?\s+to|"
    r"promised|requested|required\s+to|will)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class GmailFactQualityDecision:
    disposition: str
    reason: str | None = None


def evaluate_gmail_fact_quality(
    *,
    source_type: str,
    source_tags: Any,
    statement: str,
    claim_class: str,
    evidence_text: str,
    entities: list[dict[str, Any]] | None = None,
) -> GmailFactQualityDecision:
    """Apply bounded Gmail-only semantic and durability gates before actions exist.

    The gate intentionally recognizes only high-precision surface forms. Ambiguous
    semantics remain the critic's job; deterministic code must not silently rewrite
    a claim into a different speech act.
    """

    if source_type != "gmail_thread":
        return GmailFactQualityDecision("accept")
    statement = _bounded_text(statement, 4_000)
    evidence_text = _bounded_text(evidence_text, 24_000)

    if (
        claim_class == "commitment"
        and (
            _REQUEST_REPORTING_STATEMENT.search(statement)
            or (
                _REQUEST_EVIDENCE.search(evidence_text)
                and not _ACCEPTED_COMMITMENT_EVIDENCE.search(evidence_text)
            )
        )
    ):
        return GmailFactQualityDecision(
            "reject",
            "gmail_claim_class_mismatch: request evidence does not establish a commitment",
        )

    conditional_instruction = bool(
        _CONDITIONAL_SCHEDULING_INSTRUCTION.search(evidence_text)
        or (
            _CONDITIONAL_DECLARATIVE_STATEMENT.search(statement)
            and "?" not in statement
            and not _UNRESOLVED_QUESTION_STATEMENT.search(statement)
        )
    )
    if claim_class == "open_question" and conditional_instruction:
        return GmailFactQualityDecision(
            "drop", "gmail_non_durable_conditional_scheduling_instruction"
        )

    if _omits_context_from_general_rule(statement, evidence_text, entities):
        return GmailFactQualityDecision(
            "reject", "gmail_context_qualifier_omitted_from_general_rule"
        )

    tags = _normalized_tags(source_tags)
    text = f"{statement}\n{evidence_text}"
    primary_event = _primary_event(entities)
    one_off_event = primary_event or bool(_ONE_OFF_EVENT_NOUN.search(text))
    transactional = "gmail:delivery:transactional" in tags

    if (
        _ACCESS_DETAIL.search(statement)
        and not _durable_capability_statement(statement, entities)
        and (
            primary_event
            or _ONE_OFF_EVENT_NOUN.search(statement)
            or _CONCRETE_ACCESS_VALUE.search(statement)
        )
    ):
        return GmailFactQualityDecision("drop", "gmail_non_durable_access_detail")

    if (
        transactional
        and one_off_event
        and _ROUTINE_EVENT_ATTRIBUTE.search(statement)
        and not _is_core_event_fact(statement, claim_class)
    ):
        return GmailFactQualityDecision(
            "drop", "gmail_non_durable_one_off_event_attribute"
        )

    return GmailFactQualityDecision("accept")


def gmail_fact_quality_prompt_rule() -> str:
    return (
        "A request is not a commitment unless the cited reply explicitly accepts it. "
        "A conditional instruction about how to schedule is not an open question and "
        "is not a long-term fact. Preserve jurisdiction, reservation, trip, and other "
        "context qualifiers; never restate a context-specific rule as universal. Omit "
        "one-off access details and routine booking attributes such as references, "
        "codes, guest counts, room or bed details, and payment or payout amounts. Keep "
        "core event identity, schedule, and source-backed obligation facts.\n"
    )


def _omits_context_from_general_rule(
    statement: str,
    evidence_text: str,
    entities: list[dict[str, Any]] | None,
) -> bool:
    if not _GENERIC_RULE_STATEMENT.search(statement):
        return False
    legal_contexts = [
        match.group("context") for match in _LEGAL_CONTEXT.finditer(evidence_text)
    ]
    for legal_context in legal_contexts:
        jurisdiction = re.sub(
            r"\b(?:law|laws|regulation|regulations|rule|rules|statute|statutes)\b.*$",
            "",
            legal_context,
            flags=re.IGNORECASE,
        ).strip(" ,:-")
        jurisdiction = re.sub(r"^(?:applicable|local|the)\s+", "", jurisdiction)
        if jurisdiction:
            if jurisdiction.casefold() not in statement.casefold():
                return True
        elif not re.search(
            r"\b(?:law|legal|jurisdiction|regulation|statute)\b",
            statement,
            re.IGNORECASE,
        ):
            return True

    if (
        _SOURCE_SCOPED_NORMATIVE_RULE.search(statement)
        and not _statement_has_source_authority(statement, entities)
    ):
        return True

    return bool(
        _EVIDENCE_ONE_OFF_CONTEXT.search(evidence_text)
        and not _STATEMENT_CONTEXT_QUALIFIER.search(statement)
    )


def _statement_has_source_authority(
    statement: str, entities: list[dict[str, Any]] | None
) -> bool:
    if _SOURCE_AUTHORITY_QUALIFIER.search(statement):
        return True
    normalized_statement = statement.casefold()
    return any(
        isinstance(entity, dict)
        and str(entity.get("type") or entity.get("entity_type") or "").casefold()
        in {"organization", "product"}
        and len(surface := str(entity.get("surface") or "").strip()) >= 3
        and surface.casefold() in normalized_statement
        for entity in entities or []
    )


def _is_core_event_fact(statement: str, claim_class: str) -> bool:
    if claim_class == "decision" or _OBLIGATION.search(statement):
        return True
    if _EVENT_IDENTITY.search(statement):
        return True
    return bool(_SCHEDULE_PREDICATE.search(statement) and _EXPLICIT_TIME.search(statement))


def _primary_event(entities: list[dict[str, Any]] | None) -> bool:
    return any(
        isinstance(entity, dict)
        and str(entity.get("type") or entity.get("entity_type") or "").casefold()
        == "event"
        and bool(entity.get("is_primary"))
        for entity in entities or []
    )


def _durable_capability_statement(
    statement: str, entities: list[dict[str, Any]] | None
) -> bool:
    primary_types = {
        str(entity.get("type") or entity.get("entity_type") or "").casefold()
        for entity in entities or []
        if isinstance(entity, dict) and bool(entity.get("is_primary"))
    }
    return bool(
        primary_types & {"organization", "product", "project"}
        and _CAPABILITY_PREDICATE.search(statement)
        and not _CONCRETE_ACCESS_VALUE.search(statement)
    )


def _normalized_tags(value: Any) -> set[str]:
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            parsed = []
    else:
        parsed = value
    if not isinstance(parsed, (list, tuple, set)):
        return set()
    return {str(item).strip().casefold() for item in parsed if str(item).strip()}


def _bounded_text(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    half = limit // 2
    return f"{value[:half]}\n{value[-half:]}"
