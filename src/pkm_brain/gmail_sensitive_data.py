from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any


GMAIL_SENSITIVE_DATA_VERSION = 2
GMAIL_SENSITIVE_MASK = "█"
_NON_SECRET_LABEL_VALUES = {
    "above",
    "attached",
    "available",
    "below",
    "changed",
    "expired",
    "generated",
    "included",
    "incorrect",
    "invalid",
    "missing",
    "needed",
    "optional",
    "pending",
    "provided",
    "required",
    "reset",
    "sent",
    "shown",
    "there",
    "this",
    "unavailable",
    "unknown",
}


@dataclass(frozen=True)
class GmailSensitiveRedaction:
    kind: str
    start: int
    end: int


@dataclass(frozen=True)
class SanitizedGmailText:
    text: str
    redactions: tuple[GmailSensitiveRedaction, ...]


_LABELED_SECRET_RE = re.compile(
    r"""
    \b(?P<label>
        (?:(?:zoom|webex|teams|meeting)\s+)?(?:passcode|password)
        |pin
        |(?:one[\s-]?time|verification|security|access|sign[\s-]?in|login|authentication|temporary(?:\s+(?:security|access|sign[\s-]?in|login|authentication))?)\s+(?:password|passcode|code)
        |otp
        |confirmation\s+(?:code|number)
        |booking\s+(?:reference|code|number)
        |reservation\s+(?:code|number)
        |record\s+locator
    )\b
    (?:\s*(?::|=)\s*|\s+(?:is|was)(?:\s*:\s*|\s+)|[ \t]*\n+[ \t]*)
    (?P<value>[^\s<>{}\[\](),;|\"']{4,256})
    """,
    re.IGNORECASE | re.VERBOSE,
)
_TEMPORARY_PASSWORD_LINE_RE = re.compile(
    r"\btemporary(?:\s+(?:security|access|sign[\s-]?in|login|authentication))?\s+"
    r"(?:password|passcode)\b"
    r"(?:\s*(?::|=)\s*|\s+(?:is|was)(?:\s*:\s*|\s+)|[ \t]*\n+[ \t]*)"
    r"(?P<value>[^\r\n]{4,256})",
    re.IGNORECASE,
)
_NUMERIC_LABELED_SECRET_RE = re.compile(
    r"\b(?:one[\s-]?time|verification|security|access|sign[\s-]?in|login|authentication|temporary(?:\s+(?:security|access|sign[\s-]?in|login|authentication))?)\s+"
    r"(?:password|passcode|code)\b"
    r"(?:\s*(?::|=)\s*|\s+(?:is|was)(?:\s*:\s*|\s+)|\s+)"
    r"(?P<value>\d(?:[ \t-]*\d){3,11})\b",
    re.IGNORECASE,
)
_NUMERIC_PASSCODE_RE = re.compile(
    r"\b(?:passcode|pin|otp)\b"
    r"(?:\s*(?::|=|[-–—])\s*|\s+(?:is|was)(?:\s*:\s*|\s+)|\s+)"
    r"(?P<value>\d(?:[ \t-]*\d){3,11})\b",
    re.IGNORECASE,
)
_YOUR_NUMERIC_CODE_RE = re.compile(
    r"\byour\s+(?:(?:one[\s-]?time|verification|security|access|sign[\s-]?in|login|authentication|temporary(?:\s+(?:security|access|sign[\s-]?in|login|authentication))?)\s+)?code\b"
    r"(?:\s*(?::|=)\s*|\s+(?:is|was)(?:\s*:\s*|\s+)|\s+)"
    r"(?P<value>\d(?:[ \t-]*\d){3,11})\b",
    re.IGNORECASE,
)
_GENERIC_NUMERIC_CODE_RE = re.compile(
    r"\bcode\b"
    r"(?:\s*(?::|=)\s*|\s+(?:is|was)(?:\s*:\s*|\s+))"
    r"(?P<value>\d(?:[ \t-]*\d){5,11})\b",
    re.IGNORECASE,
)
_ACCOUNT_CODE_PHRASE_RE = re.compile(
    r"\b(?:the\s+)?code\s+for\s+(?:your|the)\s+"
    r"(?:(?:apple|microsoft|google)\s+)?account\s+"
    r"(?:is|was)(?:\s*:\s*|\s+)"
    r"(?P<value>\d(?:[ \t-]*\d){3,11})\b",
    re.IGNORECASE,
)
_VERIFICATION_NUMBER_RE = re.compile(
    r"\b(?:your\s+)?verification\s+number\b"
    r"(?:\s*(?::|=)\s*|\s+(?:is|was)(?:\s*:\s*|\s+)|\s+)"
    r"(?P<value>\d(?:[ \t-]*\d){3,11})\b",
    re.IGNORECASE,
)
_VERIFICATION_NUMBER_SUFFIX_RE = re.compile(
    r"\b(?P<value>\d(?:[ \t-]*\d){3,11})\s+"
    r"(?:as|is|was)\s+(?:your\s+|the\s+)?verification\s+number\b",
    re.IGNORECASE,
)
_DIRECT_LOCATOR_RE = re.compile(
    r"(?i:\b(?:"
    r"(?:confirmation\s+(?:code|number)|booking\s+(?:reference|code|number)|"
    r"reservation\s+(?:code|number)|record\s+locator)\b"
    r"(?:\s*(?:#|:|=)\s*|\s+(?:is|was)(?:\s*:\s*|\s+)|\s+)"
    r"|(?:confirmation|reservation)\s*#\s*"
    r"))"
    r"(?P<value>(?:\d(?:[ \t-]*\d){3,15}|[A-Z0-9][A-Z0-9_-]{4,31})\b[.,]?)"
)
_SUFFIXED_SECRET_RE = re.compile(
    r"\b(?P<value>\d(?:[ \t-]*\d){3,11}|[A-Z0-9][A-Z0-9_-]{4,31})"
    r"\s+(?:as|is|was)\s+(?:your\s+|the\s+)?(?:one[\s-]?time\s+)?"
    r"(?:verification|security|access|sign[\s-]?in|login|authentication|temporary(?:\s+(?:security|access|sign[\s-]?in|login|authentication))?)?\s*(?:password|passcode|code|pin|otp|"
    r"confirmation\s+(?:code|number)|booking\s+(?:reference|code|number)|"
    r"reservation\s+(?:code|number)|record\s+locator)\b",
    re.IGNORECASE,
)
_AUTH_ACTION_CODE_RE = re.compile(
    r"\b(?:use|enter)\s+(?:this\s+)?"
    r"(?:code(?:\s*(?::|=)\s*|\s+))?"
    r"(?P<value>\d(?:[ \t-]*\d){3,11})\s+"
    r"(?:to|for)\s+(?:sign[\s-]?in|log[\s-]?in|authenticate|verify)\b",
    re.IGNORECASE,
)
_CONTINUE_CODE_RE = re.compile(
    r"\b(?:use|enter)\s+(?:this\s+)?code\s+"
    r"to\s+(?:continue|proceed|complete\s+(?:sign[\s-]?in|login|verification))"
    r"\s*(?::|=|[-–—])\s*"
    r"(?P<value>\d(?:[ \t-]*\d){3,11})\b",
    re.IGNORECASE,
)
_ACCOUNT_CODE_LABEL_RE = re.compile(
    r"\b(?:your\s+)?(?:apple\s+id|microsoft\s+account|google\s+account)\s+"
    r"(?:(?:verification|security|access|sign[\s-]?in|login|authentication)\s+)?"
    r"(?:password|passcode|code)\b"
    r"(?:\s*(?::|=)\s*|\s+(?:is|was)(?:\s*:\s*|\s+)|\s+)"
    r"(?P<value>\d(?:[ \t-]*\d){3,11})\b",
    re.IGNORECASE,
)
_ACCOUNT_CODE_SUFFIX_RE = re.compile(
    r"\b(?P<value>\d(?:[ \t-]*\d){3,11})\s+"
    r"(?:as|is|was)\s+(?:your\s+|the\s+)?"
    r"(?:apple\s+id|microsoft\s+account|google\s+account)\s+"
    r"(?:(?:verification|security|access|sign[\s-]?in|login|authentication)\s+)?"
    r"(?:password|passcode|code)\b",
    re.IGNORECASE,
)
_MEETING_ID_RE = re.compile(
    r"\bmeeting\s+id\b"
    r"(?:\s*(?::|=)\s*|\s+(?:is|was)(?:\s*:\s*|\s+)|\s+)"
    r"(?P<value>\d(?:[ \t-]*\d){7,15})",
    re.IGNORECASE,
)
_AUTH_HEADER_RE = re.compile(
    r"\b(?:authorization|proxy-authorization)\s*:\s*(?:bearer|basic)\s+"
    r"(?P<value>[A-Za-z0-9._~+/=-]{8,512})",
    re.IGNORECASE,
)
_SENSITIVE_KEY_VALUE_RE = re.compile(
    r"\b(?:access[_-]?token|refresh[_-]?token|id[_-]?token|auth[_-]?token|"
    r"api[_-]?key|client[_-]?secret|aws[_-]?secret[_-]?access[_-]?key|"
    r"secret[_-]?access[_-]?key|session[_-]?(?:id|token)|private[_-]?key)\b"
    r"[\"']?\s*(?::|=)\s*[\"']?(?P<value>[A-Za-z0-9._~+/=-]{8,512})",
    re.IGNORECASE,
)
_SENSITIVE_QUERY_RE = re.compile(
    r"(?P<prefix>[?&#](?:pwd|passcode|password|token|token_hash|confirmation_token|access_token|refresh_token|"
    r"id_token|auth|authorization|signature|sig|api_key|secret|code|otp|ticket|"
    r"login_token|signin_token|sign_in_token|authentication_token|"
    r"verification_token|security_token|magic_token|magic_link_token|"
    r"reset_token|password_reset_token|reset_password_token|"
    r"login_code|signin_code|sign_in_code|access_code|oobcode|actioncode|"
    r"session|sessionid|euid|meeting_id|meetingid|mtid|oauth_token|"
    r"oauth_verifier|samlresponse|"
    r"x-amz-signature|x-amz-credential|x-amz-security-token|x-goog-signature|"
    r"x-goog-credential|x-goog-security-token)=)(?P<value>[^&#\s<>\"']+)",
    re.IGNORECASE,
)
_OPAQUE_AUTH_PATH_RE = re.compile(
    r"https?://[A-Za-z0-9.-]+(?::\d+)?"
    r"(?:/[A-Za-z0-9._~%+-]+){0,5}/"
    r"(?:magic(?:[-_]?link)?|reset(?:[-_]?password)?|password[-_]?reset|"
    r"login|log[-_]?in|signin|sign[-_]?in|verify|verify[-_]?email|verification|"
    r"confirm[-_]?email|activate|authenticate|authentication)"
    r"(?:/(?:token|code|link))?/"
    r"(?P<value>[A-Za-z0-9._~%+=-]{8,512})",
    re.IGNORECASE,
)
_OPAQUE_AUTH_QUERY_RE = re.compile(
    r"https?://[A-Za-z0-9.-]+(?::\d+)?"
    r"(?=[^?#\s<>\"']{0,512}/(?:magic(?:[-_]?link)?|"
    r"reset(?:[-_]?password)?|password[-_]?reset|login|log[-_]?in|"
    r"signin|sign[-_]?in|verify|verify[-_]?email|verification|"
    r"confirm[-_]?email|activate|auth|authenticate|authentication)(?:[/_.]|[?#]|$))"
    r"[^?#\s<>\"']*(?:\?[^#\s<>\"']*?&|[?#])key="
    r"(?P<value>[^&#\s<>\"']+)",
    re.IGNORECASE,
)
_OPAQUE_QUERY_RE = re.compile(
    r"(?P<prefix>[?&]c=)(?P<value>[A-Za-z0-9._%=-]{8,})",
    re.IGNORECASE,
)
_AIRBNB_RESERVATION_PATH_RE = re.compile(
    r"https?://(?:www\.)?airbnb\.com/(?:hosting/)?reservations/details/"
    r"(?P<value>[^/?#\s<>\"']+)",
    re.IGNORECASE,
)
_MEETING_ACCESS_PATH_RES = (
    re.compile(
        r"https?://(?:[A-Za-z0-9-]+\.)?zoom\.us/j/"
        r"(?P<value>\d(?:[ \t-]*\d){7,15})",
        re.IGNORECASE,
    ),
    re.compile(
        r"https?://meet\.google\.com/(?P<value>[a-z]{3}-[a-z]{4}-[a-z]{3})",
        re.IGNORECASE,
    ),
    re.compile(
        r"https?://teams\.microsoft\.com/l/meetup-join/"
        r"(?P<value>[^?#\s<>\"']+)",
        re.IGNORECASE,
    ),
)
_PRIVATE_KEY_BLOCK_RE = re.compile(
    r"(?P<value>-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----.*?"
    r"-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----)",
    re.IGNORECASE | re.DOTALL,
)
_STANDALONE_TOKEN_RES = (
    (
        "oauth_token",
        re.compile(
            r"(?P<value>GOCSPX-[A-Za-z0-9_-]{8,}|ya29\.[A-Za-z0-9._-]{8,}|"
            r"1//[A-Za-z0-9._-]{8,})"
        ),
    ),
    (
        "jwt",
        re.compile(
            r"(?<![A-Za-z0-9_-])(?P<value>[A-Za-z0-9_-]{10,4096}\."
            r"[A-Za-z0-9_-]{10,4096}\.[A-Za-z0-9_-]{10,4096})"
            r"(?![A-Za-z0-9_-])"
        ),
    ),
    (
        "provider_token",
        re.compile(
            r"(?P<value>(?:AKIA|ASIA)[0-9A-Z]{16}|"
            r"gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|"
            r"xox[baprs]-[A-Za-z0-9-]{10,}|"
            r"(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}|"
            r"AIza[0-9A-Za-z_-]{20,}|sk-[A-Za-z0-9_-]{20,})"
        ),
    ),
)


def sanitize_gmail_sensitive_text(
    value: str, *, source_values: tuple[str, ...] = ()
) -> SanitizedGmailText:
    """Mask Gmail access secrets without changing source character offsets."""
    redactions = _merge_redactions(
        [
            *gmail_sensitive_redactions(value),
            *_known_sensitive_value_redactions(value, source_values),
        ]
    )
    if not redactions:
        return SanitizedGmailText(text=value, redactions=())
    characters = list(value)
    for redaction in redactions:
        for index in range(redaction.start, redaction.end):
            if not characters[index].isspace():
                characters[index] = GMAIL_SENSITIVE_MASK
    return SanitizedGmailText(text="".join(characters), redactions=redactions)


def sanitize_gmail_model_payload(value: Any) -> Any:
    """Return a recursively sanitized copy suitable for an external model."""
    source_values = _payload_sensitive_values(value)
    return _sanitize_gmail_model_payload(value, source_values=source_values)


def sanitize_gmail_evidence_quotes(
    quotes: list[str], *, source_values: tuple[str, ...] = ()
) -> tuple[list[str], dict[str, Any] | None]:
    """Sanitize cached quotes while retaining their source-relative lengths."""
    combined_values = tuple(
        dict.fromkeys([*source_values, *_payload_sensitive_values(quotes)])
    )
    sanitized_quotes: list[str] = []
    redaction_kinds: set[str] = set()
    redaction_count = 0
    for quote in quotes:
        sanitized = sanitize_gmail_sensitive_text(quote, source_values=combined_values)
        sanitized_quotes.append(sanitized.text)
        redaction_count += len(sanitized.redactions)
        redaction_kinds.update(item.kind for item in sanitized.redactions)
    if not redaction_count:
        return sanitized_quotes, None
    return sanitized_quotes, {
        "version": GMAIL_SENSITIVE_DATA_VERSION,
        "redaction_count": redaction_count,
        "kinds": sorted(redaction_kinds),
        "source_span_offsets_preserved": True,
    }


def _sanitize_gmail_model_payload(value: Any, *, source_values: tuple[str, ...]) -> Any:
    if isinstance(value, str):
        return sanitize_gmail_sensitive_text(value, source_values=source_values).text
    if isinstance(value, dict):
        return {
            key: _sanitize_gmail_model_payload(item, source_values=source_values)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [
            _sanitize_gmail_model_payload(item, source_values=source_values)
            for item in value
        ]
    if isinstance(value, tuple):
        return tuple(
            _sanitize_gmail_model_payload(item, source_values=source_values)
            for item in value
        )
    return value


def gmail_sensitive_redactions(value: str) -> tuple[GmailSensitiveRedaction, ...]:
    candidates: list[GmailSensitiveRedaction] = []
    for kind, pattern in (
        ("labeled_secret", _LABELED_SECRET_RE),
        ("labeled_secret", _TEMPORARY_PASSWORD_LINE_RE),
        ("labeled_numeric_secret", _NUMERIC_LABELED_SECRET_RE),
        ("labeled_numeric_secret", _NUMERIC_PASSCODE_RE),
        ("labeled_numeric_secret", _YOUR_NUMERIC_CODE_RE),
        ("labeled_numeric_secret", _GENERIC_NUMERIC_CODE_RE),
        ("labeled_numeric_secret", _ACCOUNT_CODE_PHRASE_RE),
        ("labeled_numeric_secret", _VERIFICATION_NUMBER_RE),
        ("labeled_numeric_secret", _AUTH_ACTION_CODE_RE),
        ("labeled_numeric_secret", _CONTINUE_CODE_RE),
        ("labeled_numeric_secret", _ACCOUNT_CODE_LABEL_RE),
        ("suffixed_secret", _ACCOUNT_CODE_SUFFIX_RE),
        ("suffixed_secret", _VERIFICATION_NUMBER_SUFFIX_RE),
        ("access_locator", _DIRECT_LOCATOR_RE),
        ("suffixed_secret", _SUFFIXED_SECRET_RE),
        ("meeting_id", _MEETING_ID_RE),
        ("authorization", _AUTH_HEADER_RE),
        ("auth_key_value", _SENSITIVE_KEY_VALUE_RE),
        ("url_token", _SENSITIVE_QUERY_RE),
        ("opaque_auth_path_token", _OPAQUE_AUTH_PATH_RE),
        ("opaque_auth_query_token", _OPAQUE_AUTH_QUERY_RE),
        ("opaque_url_token", _OPAQUE_QUERY_RE),
        ("travel_access_locator", _AIRBNB_RESERVATION_PATH_RE),
        ("private_key", _PRIVATE_KEY_BLOCK_RE),
        *(("meeting_access_locator", pattern) for pattern in _MEETING_ACCESS_PATH_RES),
        *_STANDALONE_TOKEN_RES,
    ):
        for match in pattern.finditer(value):
            start, end = match.span("value")
            raw_value = value[start:end]
            if (
                start < end
                and not _mask_only(raw_value)
                and not _obvious_non_secret_label_value(kind, raw_value)
                and (
                    kind
                    not in {
                        "opaque_auth_path_token",
                        "opaque_auth_query_token",
                    }
                    or _looks_like_opaque_auth_path_value(raw_value)
                )
            ):
                candidates.append(
                    GmailSensitiveRedaction(kind=kind, start=start, end=end)
                )
    return _merge_redactions(candidates)


def _mask_only(value: str) -> bool:
    visible = [character for character in value if not character.isspace()]
    return bool(visible) and all(
        character == GMAIL_SENSITIVE_MASK for character in visible
    )


def _obvious_non_secret_label_value(kind: str, value: str) -> bool:
    if kind not in {"access_locator", "labeled_secret", "suffixed_secret"}:
        return False
    normalized = value.strip("\"'`.,:;!?()[]{}").casefold()
    return normalized in _NON_SECRET_LABEL_VALUES


def _looks_like_opaque_auth_path_value(value: str) -> bool:
    compact = re.sub(r"[^A-Za-z0-9]", "", value)
    if len(compact) < 8:
        return False
    if not compact.isalpha():
        return True
    return not compact.islower() or len(compact) >= 20


def gmail_sensitive_values(value: str) -> tuple[str, ...]:
    values: list[str] = []
    for redaction in gmail_sensitive_redactions(value):
        raw = value[redaction.start : redaction.end]
        compact_numeric = re.sub(r"[ \t-]", "", raw)
        candidates = [raw, raw.strip("\"'`.,:;!?()[]{}")]
        if compact_numeric.isdigit() and compact_numeric != raw:
            candidates.append(compact_numeric)
        for candidate in candidates:
            if len(candidate) >= 4 and candidate not in values:
                values.append(candidate)
    return tuple(values)


def gmail_payload_contains_sensitive_value(
    payload: Any, *, source_values: tuple[str, ...] = ()
) -> bool:
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    if gmail_sensitive_redactions(serialized):
        return True
    return any(
        _source_value_is_distinctive(value) and _source_value_occurs(value, serialized)
        for value in source_values
    )


def gmail_payload_contains_sensitive_mask(payload: Any) -> bool:
    """Return whether model-authored payload contains a redaction placeholder."""
    return GMAIL_SENSITIVE_MASK in json.dumps(
        payload, ensure_ascii=False, sort_keys=True
    )


def _payload_sensitive_values(value: Any) -> tuple[str, ...]:
    collected: list[str] = []
    if isinstance(value, str):
        for item in gmail_sensitive_values(value):
            if item not in collected:
                collected.append(item)
    elif isinstance(value, dict):
        for item in value.values():
            for sensitive in _payload_sensitive_values(item):
                if sensitive not in collected:
                    collected.append(sensitive)
    elif isinstance(value, (list, tuple)):
        for item in value:
            for sensitive in _payload_sensitive_values(item):
                if sensitive not in collected:
                    collected.append(sensitive)
    return tuple(collected)


def _known_sensitive_value_redactions(
    value: str, source_values: tuple[str, ...]
) -> list[GmailSensitiveRedaction]:
    redactions: list[GmailSensitiveRedaction] = []
    for source_value in sorted(set(source_values), key=len, reverse=True):
        if not _source_value_is_distinctive(source_value):
            continue
        compact = re.sub(r"[^A-Za-z0-9]", "", source_value)
        flags = 0 if compact.isalpha() else re.IGNORECASE
        for match in re.finditer(re.escape(source_value), value, flags):
            redactions.append(
                GmailSensitiveRedaction(
                    kind="source_sensitive_value",
                    start=match.start(),
                    end=match.end(),
                )
            )
    return redactions


def _source_value_is_distinctive(value: str) -> bool:
    compact = re.sub(r"[^A-Za-z0-9]", "", value)
    if len(compact) < 4:
        return False
    if compact.isdigit():
        return len(compact) >= 6
    if compact.isalpha():
        # Lowercase prose immediately following a label can be an adjective or
        # instruction ("password is secure"), not a reusable secret. The
        # labeled occurrence is still masked by the primary detector. Only
        # propagate code-like uppercase/mixed-case alphabetic values elsewhere.
        return len(compact) >= 6 and not compact.islower()
    return True


def _source_value_occurs(source_value: str, value: str) -> bool:
    compact = re.sub(r"[^A-Za-z0-9]", "", source_value)
    if compact.isalpha():
        return source_value in value
    return source_value.casefold() in value.casefold()


def _merge_redactions(
    values: list[GmailSensitiveRedaction],
) -> tuple[GmailSensitiveRedaction, ...]:
    if not values:
        return ()
    ordered = sorted(
        values,
        key=lambda item: (
            item.start,
            -item.end,
            item.kind == "source_sensitive_value",
            item.kind,
        ),
    )
    merged: list[GmailSensitiveRedaction] = []
    for item in ordered:
        if not merged or item.start >= merged[-1].end:
            merged.append(item)
            continue
        previous = merged[-1]
        previous_contains_item = (
            previous.start <= item.start and previous.end >= item.end
        )
        item_contains_previous = (
            item.start <= previous.start and item.end >= previous.end
        )
        merged[-1] = GmailSensitiveRedaction(
            kind=(
                previous.kind
                if previous.kind == item.kind
                or (previous.start == item.start and previous.end == item.end)
                or (item.kind == "source_sensitive_value" and previous_contains_item)
                else item.kind
                if previous.kind == "source_sensitive_value" and item_contains_previous
                else "multiple_sensitive_values"
            ),
            start=previous.start,
            end=max(previous.end, item.end),
        )
    return tuple(merged)
