from __future__ import annotations

import base64
import binascii
import copy
import email.header
import email.utils
import html
import re
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Any, Iterable, Mapping


CALENDAR_TITLE_CAP = 500
CALENDAR_DETAILS_CAP = 4_000
GMAIL_MESSAGE_BODY_CAP = 30_000
GMAIL_THREAD_BODY_CAP = 120_000
HEADER_CAP = 2_000

_REPLY_MARKERS = (
    re.compile(r"^\s*On .{1,500} wrote:\s*$", re.IGNORECASE),
    re.compile(r"^\s*-{2,}\s*Original Message\s*-{2,}\s*$", re.IGNORECASE),
    re.compile(r"^\s*Begin forwarded message:\s*$", re.IGNORECASE),
    re.compile(r"^\s*_{5,}\s*$"),
)
_HEADER_QUOTE_START = re.compile(r"^\s*(?:From|Sent):\s+.+$", re.IGNORECASE)
_CHARSET = re.compile(r"charset\s*=\s*[\"']?([^;\"'\s]+)", re.IGNORECASE)
_TRANSACTIONAL_SUBJECT = re.compile(
    r"\b(receipt|invoice|order|shipp(?:ed|ing)|delivery|verification|security alert|"
    r"password|statement|payment|reservation|confirmation|renewal|expires?|due)\b",
    re.IGNORECASE,
)
_MARKETING_HEADERS = {"list-unsubscribe", "list-id"}
_NO_REPLY = re.compile(
    r"(?:^|[<\s])(?:no[-_.]?reply|do[-_.]?not[-_.]?reply|notifications?|alerts?)@",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class NormalizedCalendarEvent:
    event_id: str
    etag: str | None
    source_revision: str | None
    status: str
    title: str
    details: str | None
    location: str | None
    created_at: str | None
    updated_at: str | None
    starts_at: str | None
    start_date: str | None
    ends_at: str | None
    end_date: str | None
    source_timezone: str | None
    recurrence: tuple[str, ...]
    recurring_event_id: str | None
    original_start_time: str | None
    original_start_date: str | None
    sequence: int | None
    visibility: str | None
    transparency: str | None
    event_type: str | None
    organizer_email: str | None
    organizer_self: bool
    attendee_count: int
    attendee_response: str | None
    cancelled: bool

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class NormalizedGmailMessage:
    message_id: str
    thread_id: str
    internal_date: str | None
    timestamp: str | None
    from_addresses: tuple[str, ...]
    to_addresses: tuple[str, ...]
    cc_addresses: tuple[str, ...]
    subject: str | None
    date_header: str | None
    internet_message_id: str | None
    in_reply_to: str | None
    references: tuple[str, ...]
    label_ids: tuple[str, ...]
    outgoing: bool
    operator_authored: bool
    body: str
    body_kind: str | None
    attachment_count: int
    quoted_chars_removed: int
    truncated: bool

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class NormalizedGmailThread:
    thread_id: str
    history_id: str | None
    source_revision: str | None
    subject: str | None
    created_at: str | None
    updated_at: str | None
    message_class: str
    messages: tuple[NormalizedGmailMessage, ...]
    body_chars: int
    attachment_count: int
    quoted_chars_removed: int
    truncated: bool

    def as_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["messages"] = [message.as_dict() for message in self.messages]
        return value


def normalize_calendar_event(value: Mapping[str, Any]) -> NormalizedCalendarEvent:
    event_id = _bounded(value.get("id"), 1_000)
    if not event_id:
        raise ValueError("Calendar event is missing id")
    status = _bounded(value.get("status"), 100) or "confirmed"
    visibility = _bounded(value.get("visibility"), 100)
    private = visibility in {"private", "confidential"}
    start = value.get("start") if isinstance(value.get("start"), Mapping) else {}
    end = value.get("end") if isinstance(value.get("end"), Mapping) else {}
    original = (
        value.get("originalStartTime")
        if isinstance(value.get("originalStartTime"), Mapping)
        else {}
    )
    organizer = (
        value.get("organizer") if isinstance(value.get("organizer"), Mapping) else {}
    )
    attendees = [
        attendee
        for attendee in value.get("attendees") or []
        if isinstance(attendee, Mapping)
    ]
    self_attendee = next(
        (attendee for attendee in attendees if attendee.get("self") is True),
        None,
    )
    title = "Private event" if private else (_bounded(value.get("summary"), CALENDAR_TITLE_CAP) or "Untitled event")
    details = None if private else _bounded(value.get("description"), CALENDAR_DETAILS_CAP)
    location = None if private else _bounded(value.get("location"), 1_000)
    return NormalizedCalendarEvent(
        event_id=event_id,
        etag=_bounded(value.get("etag"), 1_000),
        source_revision=_bounded(value.get("etag"), 1_000),
        status=status,
        title=title,
        details=details,
        location=location,
        created_at=_bounded(value.get("created"), 100),
        updated_at=_bounded(value.get("updated"), 100),
        starts_at=_bounded(start.get("dateTime"), 100),
        start_date=_bounded(start.get("date"), 100),
        ends_at=_bounded(end.get("dateTime"), 100),
        end_date=_bounded(end.get("date"), 100),
        source_timezone=(
            _bounded(start.get("timeZone"), 200)
            or _bounded(end.get("timeZone"), 200)
        ),
        recurrence=tuple(
            normalized
            for item in value.get("recurrence") or []
            if (normalized := _bounded(item, 2_000))
        ),
        recurring_event_id=_bounded(value.get("recurringEventId"), 1_000),
        original_start_time=_bounded(original.get("dateTime"), 100),
        original_start_date=_bounded(original.get("date"), 100),
        sequence=_optional_int(value.get("sequence")),
        visibility=visibility,
        transparency=_bounded(value.get("transparency"), 100),
        event_type=_bounded(value.get("eventType"), 100),
        organizer_email=_bounded(organizer.get("email"), 500),
        organizer_self=organizer.get("self") is True,
        attendee_count=len(attendees),
        attendee_response=(
            _bounded(self_attendee.get("responseStatus"), 100)
            if self_attendee is not None
            else None
        ),
        cancelled=status == "cancelled",
    )


def normalize_gmail_thread(
    value: Mapping[str, Any],
    *,
    message_body_cap: int = GMAIL_MESSAGE_BODY_CAP,
    thread_body_cap: int = GMAIL_THREAD_BODY_CAP,
    operator_emails: Iterable[str] = (),
) -> NormalizedGmailThread:
    thread_id = _bounded(value.get("id"), 1_000)
    if not thread_id:
        raise ValueError("Gmail thread is missing id")
    raw_messages = [
        message
        for message in value.get("messages") or []
        if isinstance(message, Mapping)
    ]
    normalized_operator_emails = {
        str(address).strip().casefold() for address in operator_emails if str(address).strip()
    }
    messages = [
        normalize_gmail_message(
            message,
            message_body_cap=message_body_cap,
            operator_emails=normalized_operator_emails,
        )
        for message in raw_messages
    ]
    messages.sort(key=lambda item: _internal_date_sort_key(item.internal_date))
    messages = _cap_thread_bodies(messages, thread_body_cap)
    subject = next(
        (message.subject for message in reversed(messages) if message.subject),
        None,
    )
    dates = [
        parsed
        for message in messages
        if (parsed := _optional_epoch_millis(message.internal_date)) is not None
    ]
    return NormalizedGmailThread(
        thread_id=thread_id,
        history_id=_bounded(value.get("historyId"), 100),
        source_revision=_bounded(value.get("historyId"), 100),
        subject=subject,
        created_at=_epoch_millis_iso(str(min(dates))) if dates else None,
        updated_at=_epoch_millis_iso(str(max(dates))) if dates else None,
        message_class=_gmail_message_class(raw_messages, subject),
        messages=tuple(messages),
        body_chars=sum(len(message.body) for message in messages),
        attachment_count=sum(message.attachment_count for message in messages),
        quoted_chars_removed=sum(message.quoted_chars_removed for message in messages),
        truncated=any(message.truncated for message in messages),
    )


def normalize_gmail_message(
    value: Mapping[str, Any],
    *,
    message_body_cap: int = GMAIL_MESSAGE_BODY_CAP,
    operator_emails: Iterable[str] = (),
) -> NormalizedGmailMessage:
    message_id = _bounded(value.get("id"), 1_000)
    thread_id = _bounded(value.get("threadId"), 1_000)
    if not message_id or not thread_id:
        raise ValueError("Gmail message is missing id or threadId")
    payload = value.get("payload") if isinstance(value.get("payload"), Mapping) else {}
    headers = _gmail_headers(payload)
    body, body_kind, attachment_count = _gmail_body(payload)
    body, removed = strip_quoted_history(body)
    truncated = len(body) > message_body_cap
    if truncated:
        body = body[:message_body_cap].rstrip()
    from_addresses = _email_addresses(headers.get("from"))
    labels = tuple(str(item) for item in value.get("labelIds") or [] if item)
    normalized_operator_emails = {
        str(address).strip().casefold() for address in operator_emails if str(address).strip()
    }
    operator_authored = bool(normalized_operator_emails.intersection(from_addresses))
    outgoing = "SENT" in labels or operator_authored
    internal_date = _bounded(value.get("internalDate"), 100)
    return NormalizedGmailMessage(
        message_id=message_id,
        thread_id=thread_id,
        internal_date=internal_date,
        timestamp=_epoch_millis_iso(internal_date) if internal_date else None,
        from_addresses=from_addresses,
        to_addresses=_email_addresses(headers.get("to")),
        cc_addresses=_email_addresses(headers.get("cc")),
        subject=_decoded_header(headers.get("subject")),
        date_header=_bounded(headers.get("date"), HEADER_CAP),
        internet_message_id=_bounded(headers.get("message-id"), HEADER_CAP),
        in_reply_to=_bounded(headers.get("in-reply-to"), HEADER_CAP),
        references=tuple(
            (_bounded(item, HEADER_CAP) or "")
            for item in str(headers.get("references") or "").split()[:100]
        ),
        label_ids=labels,
        outgoing=outgoing,
        operator_authored=operator_authored,
        body=body,
        body_kind=body_kind,
        attachment_count=attachment_count,
        quoted_chars_removed=removed,
        truncated=truncated,
    )


def sanitize_gmail_thread_payload(value: Mapping[str, Any]) -> dict[str, Any]:
    """Remove attachment bytes while retaining provider identity and text MIME bodies."""

    output = copy.deepcopy(dict(value))
    for message in output.get("messages") or []:
        if isinstance(message, dict) and isinstance(message.get("payload"), dict):
            _strip_attachment_data(message["payload"])
    return output


def sanitize_calendar_event_payload(value: Mapping[str, Any]) -> dict[str, Any]:
    output = copy.deepcopy(dict(value))
    output.pop("extendedProperties", None)
    return output


def strip_quoted_history(value: str) -> tuple[str, int]:
    if not value:
        return "", 0
    retained: list[str] = []
    lines = value.splitlines()
    for index, line in enumerate(lines):
        if any(pattern.match(line) for pattern in _REPLY_MARKERS):
            break
        if _HEADER_QUOTE_START.match(line) and _looks_like_header_quote(lines[index : index + 5]):
            break
        if line.lstrip().startswith(">"):
            continue
        retained.append(line.rstrip())
    normalized = _normalize_whitespace("\n".join(retained))
    return normalized, max(0, len(value) - len(normalized))


class _EmailHTMLParser(HTMLParser):
    BLOCKS = {
        "address",
        "article",
        "aside",
        "br",
        "div",
        "footer",
        "h1",
        "h2",
        "h3",
        "h4",
        "header",
        "li",
        "p",
        "section",
        "table",
        "tr",
    }
    ALWAYS_IGNORE = {"head", "script", "style", "svg"}
    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.depth = 0
        self.ignore_boundaries: list[tuple[str, int]] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        attr = {key.casefold(): str(value or "") for key, value in attrs}
        classes = attr.get("class", "").casefold().split()
        normalized_tag = tag.casefold()
        if normalized_tag not in self.VOID:
            self.depth += 1
        should_ignore = (
            tag.casefold() in self.ALWAYS_IGNORE | {"blockquote"}
            or "gmail_quote" in classes
            or attr.get("type", "").casefold() == "cite"
        )
        if should_ignore:
            self.ignore_boundaries.append((normalized_tag, self.depth))
            return
        if not self.ignore_boundaries and normalized_tag in self.BLOCKS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        normalized_tag = tag.casefold()
        ignored = bool(self.ignore_boundaries)
        if (
            self.ignore_boundaries
            and self.ignore_boundaries[-1] == (normalized_tag, self.depth)
        ):
            self.ignore_boundaries.pop()
        self.depth = max(0, self.depth - 1)
        if not ignored and normalized_tag in self.BLOCKS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.ignore_boundaries:
            self.parts.append(data)

    def text(self) -> str:
        return _normalize_whitespace(html.unescape("".join(self.parts)))


def _gmail_body(payload: Mapping[str, Any]) -> tuple[str, str | None, int]:
    plain: list[str] = []
    html_parts: list[str] = []
    attachment_count = 0
    for part, blocked_by_attachment, attachment_root in _walk_mime_parts(payload):
        if attachment_root:
            attachment_count += 1
        if blocked_by_attachment:
            continue
        mime_type = str(part.get("mimeType") or "").casefold()
        if mime_type not in {"text/plain", "text/html"}:
            continue
        text = _decode_part_text(part)
        if not text:
            continue
        if mime_type == "text/plain":
            plain.append(text)
        else:
            html_parts.append(text)
    if plain:
        return _normalize_whitespace("\n\n".join(plain)), "text/plain", attachment_count
    if html_parts:
        parser = _EmailHTMLParser()
        parser.feed("\n".join(html_parts))
        parser.close()
        return parser.text(), "text/html", attachment_count
    return "", None, attachment_count


def _walk_mime_parts(
    payload: Mapping[str, Any],
    *,
    attachment_ancestor: bool = False,
) -> Iterable[tuple[Mapping[str, Any], bool, bool]]:
    """Walk every MIME node while carrying the attachment security boundary.

    Gmail can expand an attached RFC-822 message (or another multipart attachment)
    into ordinary-looking text descendants. Those descendants are attachment bytes,
    not correspondence in the containing thread. Count the outer attachment once and
    never expose any node below it to body decoding.
    """

    attachment_root = not attachment_ancestor and _is_attachment(payload)
    blocked_by_attachment = attachment_ancestor or attachment_root
    yield payload, blocked_by_attachment, attachment_root
    for part in payload.get("parts") or []:
        if isinstance(part, Mapping):
            yield from _walk_mime_parts(
                part,
                attachment_ancestor=blocked_by_attachment,
            )


def _is_attachment(part: Mapping[str, Any]) -> bool:
    mime_type = str(part.get("mimeType") or "").split(";", 1)[0].strip().casefold()
    if mime_type == "message/rfc822":
        return True
    if str(part.get("filename") or "").strip():
        return True
    body = part.get("body") if isinstance(part.get("body"), Mapping) else {}
    if body.get("attachmentId"):
        return True
    headers = _gmail_headers(part)
    disposition = str(headers.get("content-disposition") or "").casefold()
    return "attachment" in disposition


def _decode_part_text(part: Mapping[str, Any]) -> str:
    body = part.get("body") if isinstance(part.get("body"), Mapping) else {}
    encoded = str(body.get("data") or "")
    if not encoded:
        return ""
    try:
        padded = encoded + "=" * (-len(encoded) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
    except (UnicodeEncodeError, binascii.Error, ValueError):
        return ""
    headers = _gmail_headers(part)
    content_type = str(headers.get("content-type") or "")
    match = _CHARSET.search(content_type)
    charset = match.group(1) if match else "utf-8"
    try:
        return raw.decode(charset, errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")


def _gmail_headers(payload: Mapping[str, Any]) -> dict[str, str]:
    output: dict[str, str] = {}
    for value in payload.get("headers") or []:
        if not isinstance(value, Mapping):
            continue
        name = str(value.get("name") or "").strip().casefold()
        if name and name not in output:
            output[name] = str(value.get("value") or "")[:HEADER_CAP]
    return output


def _decoded_header(value: str | None) -> str | None:
    if not value:
        return None
    try:
        decoded = str(email.header.make_header(email.header.decode_header(value)))
    except (LookupError, UnicodeError):
        decoded = value
    return _bounded(decoded, HEADER_CAP)


def _email_addresses(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    return tuple(
        address.casefold()
        for _, address in email.utils.getaddresses([value])
        if address
    )


def _cap_thread_bodies(
    messages: list[NormalizedGmailMessage],
    cap: int,
) -> list[NormalizedGmailMessage]:
    if cap < 0:
        raise ValueError("thread body cap cannot be negative")
    remaining = cap
    newest_first: list[NormalizedGmailMessage] = []
    for message in reversed(messages):
        if len(message.body) <= remaining:
            newest_first.append(message)
            remaining -= len(message.body)
            continue
        newest_first.append(
            replace(
                message,
                body=message.body[:remaining].rstrip() if remaining else "",
                truncated=True,
            )
        )
        remaining = 0
    return list(reversed(newest_first))


def _gmail_message_class(
    messages: list[Mapping[str, Any]],
    subject: str | None,
) -> str:
    headers = [
        _gmail_headers(
            message.get("payload")
            if isinstance(message.get("payload"), Mapping)
            else {}
        )
        for message in messages
    ]
    if any(
        "SENT" in {str(label) for label in message.get("labelIds") or []}
        for message in messages
    ):
        return "human"
    if any(_MARKETING_HEADERS.intersection(message_headers) for message_headers in headers):
        return "marketing"
    senders = " ".join(message_headers.get("from", "") for message_headers in headers)
    if _NO_REPLY.search(senders) or _TRANSACTIONAL_SUBJECT.search(subject or ""):
        return "transactional"
    return "human"


def _strip_attachment_data(
    part: dict[str, Any],
    *,
    attachment_ancestor: bool = False,
) -> None:
    blocked_by_attachment = attachment_ancestor or _is_attachment(part)
    if blocked_by_attachment or not str(part.get("mimeType") or "").casefold().startswith("text/"):
        body = part.get("body")
        if isinstance(body, dict):
            body.pop("data", None)
    for child in part.get("parts") or []:
        if isinstance(child, dict):
            _strip_attachment_data(
                child,
                attachment_ancestor=blocked_by_attachment,
            )


def _looks_like_header_quote(lines: list[str]) -> bool:
    names = {
        line.split(":", 1)[0].strip().casefold()
        for line in lines
        if ":" in line
    }
    return len(names.intersection({"from", "sent", "to", "subject", "date"})) >= 3


def _normalize_whitespace(value: str) -> str:
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in value.splitlines()]
    output: list[str] = []
    blank = False
    for line in lines:
        if not line:
            if output and not blank:
                output.append("")
            blank = True
            continue
        output.append(line)
        blank = False
    return "\n".join(output).strip()


def _bounded(value: Any, cap: int) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized[:cap] if normalized else None


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _internal_date_sort_key(value: str | None) -> int:
    try:
        return int(value or 0)
    except ValueError:
        return 0


def _epoch_millis_iso(value: str) -> str | None:
    try:
        parsed = datetime.fromtimestamp(int(value) / 1_000, tz=timezone.utc)
    except (OSError, OverflowError, TypeError, ValueError):
        return None
    return parsed.replace(microsecond=0).isoformat()


def _optional_epoch_millis(value: str | None) -> int | None:
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None
