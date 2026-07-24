#!/usr/bin/env python3
"""Build a deterministic 100-case public Gmail temporal V3 fixture.

The fixture contains only fictional ``example.test`` mail.  This builder does
not ingest messages, invoke a model, or inspect a Gmail archive.  It writes one
canonical owner-only JSON file and prints aggregate counts only.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any


VERSION = "gmail_temporal_public_scale_fixture_builder_v1"
FIXTURE_VERSION = "gmail_temporal_public_challenge_fixture_v3"
CHALLENGE_ID = "gmail-temporal-public-scale-v1"
CREATED_AT = "2026-07-20T18:00:00+00:00"
MESSAGE_INTERNAL_AT = "2026-07-20T09:00:00-07:00"
ACCOUNT_EMAIL = "owner@public.example.test"
PUBLIC_DOMAIN = "public.example.test"
EXPECTED_CASES = 100
EXPECTED_POSITIVE_CASES = 60
EXPECTED_NEGATIVE_CASES = 40
HARD_NEGATIVE_PREFIX = "hard-negative-"

_PUBLIC_EMAIL_RE = re.compile(
    r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@"
    r"(?:[A-Za-z0-9-]+\.)*example\.test$",
    re.IGNORECASE,
)
_CASE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}$")
_VALUE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ISO_DATE_IN_TEXT_RE = re.compile(r"(?<!\d)\d{4}-\d{2}-\d{2}(?!\d)")
_TEXT_DATE_RE = re.compile(
    r"\b(?:January|February|March|April|May|June|July|August|September|"
    r"October|November|December) \d{1,2}, \d{4}\b"
)
_VARIANT_MARKERS = (
    "Radian",
    "Solstice",
    "Trellis",
    "Umber",
    "Vesper",
    "Windward",
    "Xylem",
    "Yonder",
)
_FIXTURE_KEYS = {
    "version",
    "challenge_id",
    "created_at",
    "message_internal_at",
    "account_email",
    "public_synthetic",
    "contains_private_gmail",
    "release_eligible",
    "cases",
}
_CASE_KEYS = {
    "case_id",
    "sender",
    "subject",
    "body",
    "label_ids",
    "members",
    "forbidden",
    "complete_group_required",
}
_MEMBER_KEYS = {
    "subject",
    "relation",
    "lifecycle",
    "value",
    "values",
    "expected_verdict",
    "canonical_subject_required",
}
_RELATIONS = {"occurrence", "deadline", "unspecified"}
_LIFECYCLES = {
    "none",
    "unknown",
    "scheduled",
    "cancelled",
    "completed",
    "rescheduled_old",
    "rescheduled_replacement",
}
_ALLOWED_LABEL_IDS = {
    "CATEGORY_PERSONAL",
    "CATEGORY_PROMOTIONS",
    "CATEGORY_UPDATES",
    "IMPORTANT",
    "INBOX",
    "SENT",
    "STARRED",
}


class PublicScaleFixtureError(ValueError):
    """Raised without reflecting fixture content or paths."""


def _canonical_json(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise PublicScaleFixtureError("fixture is not canonical JSON") from exc


def _surface(day: date) -> str:
    return f"{day.strftime('%B')} {day.day}, {day.year}"


def _value(day: date) -> str:
    return day.isoformat()


def _member(
    subject: str,
    relation: str,
    lifecycle: str,
    *,
    value: str | None = None,
    values: Sequence[str] | None = None,
    verdict: str,
    canonical: bool = False,
) -> dict[str, Any]:
    if (value is None) == (values is None):
        raise PublicScaleFixtureError("member needs exactly one value form")
    row: dict[str, Any] = {
        "subject": subject,
        "relation": relation,
        "lifecycle": lifecycle,
        "expected_verdict": verdict,
    }
    if value is not None:
        row["value"] = value
    else:
        row["values"] = list(values or ())
    if canonical:
        row["canonical_subject_required"] = True
    return row


def _forbidden(
    subject: str,
    relation: str,
    lifecycle: str,
    value: str,
) -> dict[str, str]:
    return {
        "subject": subject,
        "relation": relation,
        "lifecycle": lifecycle,
        "value": value,
    }


def _case(
    case_id: str,
    sender: str,
    subject: str,
    body: str,
    *,
    members: Sequence[Mapping[str, Any]] = (),
    forbidden: Sequence[Mapping[str, str]] = (),
    complete_group_required: bool = False,
    labels: Sequence[str] = ("CATEGORY_PERSONAL", "INBOX"),
) -> dict[str, Any]:
    return {
        "case_id": case_id,
        "sender": sender,
        "subject": subject,
        "body": body,
        "label_ids": list(labels),
        "members": [dict(item) for item in members],
        "forbidden": [dict(item) for item in forbidden],
        "complete_group_required": complete_group_required,
    }


def _positive_cases() -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    cursor = date(2026, 8, 1)

    scheduled_events = (
        "Arbor Vale interview",
        "Brindle Coast workshop",
        "Copper Finch planning session",
        "Driftwood Council meeting",
        "Everglade Studio review",
        "Foxglove Research call",
        "Granite Harbor conference",
        "Honeycomb Product demo",
        "Ironwood Fellows class",
        "Jade Lantern presentation",
        "Kite Ridge offsite",
        "Larkspur Design appointment",
    )
    schedule_phrases = (
        "is now scheduled for",
        "has been scheduled for",
        "is scheduled for",
        "was scheduled for",
    )
    for ordinal, event in enumerate(scheduled_events, start=1):
        day = cursor
        cursor += timedelta(days=1)
        phrase = schedule_phrases[(ordinal - 1) % len(schedule_phrases)]
        cases.append(
            _case(
                f"positive-schedule-{ordinal:02d}",
                f"scheduler{ordinal:02d}@{PUBLIC_DOMAIN}",
                event,
                (
                    f"The {event} {phrase} {_surface(day)}. "
                    "The organizer's latest note confirms this timing."
                ),
                members=(
                    _member(
                        event,
                        "occurrence",
                        "scheduled",
                        value=_value(day),
                        verdict="supported",
                        canonical=True,
                    ),
                ),
            )
        )

    rescheduled_events = (
        "Marble Quay roadmap review",
        "Northstar Clinic consultation",
        "Opal Grove steering meeting",
        "Prairie Bell rehearsal",
        "Quartz Harbor partner call",
        "Red Fern budget session",
        "Silverbrook training workshop",
        "Thistle Point launch briefing",
    )
    for ordinal, event in enumerate(rescheduled_events, start=1):
        old_day = cursor
        replacement_day = cursor + timedelta(days=1)
        cursor += timedelta(days=2)
        if ordinal % 2:
            body = (
                f"The {event} has been rescheduled from {_surface(old_day)} "
                f"to {_surface(replacement_day)}. Please use the replacement date."
            )
        else:
            body = (
                f"Update: the {event} is now rescheduled to {_surface(replacement_day)} "
                f"from {_surface(old_day)}."
            )
        cases.append(
            _case(
                f"positive-reschedule-{ordinal:02d}",
                f"coordinator{ordinal:02d}@{PUBLIC_DOMAIN}",
                event,
                body,
                members=(
                    _member(
                        event,
                        "occurrence",
                        "rescheduled_old",
                        value=_value(old_day),
                        verdict="supported",
                        canonical=True,
                    ),
                    _member(
                        event,
                        "occurrence",
                        "rescheduled_replacement",
                        value=_value(replacement_day),
                        verdict="supported",
                        canonical=True,
                    ),
                ),
                forbidden=(
                    _forbidden(
                        event,
                        "occurrence",
                        "rescheduled_old",
                        _value(replacement_day),
                    ),
                    _forbidden(
                        event,
                        "occurrence",
                        "rescheduled_replacement",
                        _value(old_day),
                    ),
                ),
                complete_group_required=True,
            )
        )

    cancelled_events = (
        "Umber Lake interview",
        "Verdant Gate workshop",
        "Willow Basin check-in meeting",
        "Xenon Field product demo",
        "Yarrow Commons forum",
        "Zephyr Park design review",
    )
    for ordinal, event in enumerate(cancelled_events, start=1):
        day = cursor
        cursor += timedelta(days=1)
        terminal = "has been cancelled" if ordinal % 2 else "has been called off"
        cases.append(
            _case(
                f"positive-cancellation-{ordinal:02d}",
                f"host{ordinal:02d}@{PUBLIC_DOMAIN}",
                event,
                f"The {event} scheduled for {_surface(day)} {terminal}.",
                members=(
                    _member(
                        event,
                        "occurrence",
                        "cancelled",
                        value=_value(day),
                        verdict="supported",
                        canonical=True,
                    ),
                ),
                forbidden=(_forbidden(event, "occurrence", "scheduled", _value(day)),),
            )
        )

    completed_events = (
        "Alder Crest retrospective review",
        "Blue Heron audit meeting",
        "Cobalt Meadow trial session",
        "Dune Harbor seminar",
        "Elm Arch migration review",
        "Fern Hollow inspection visit",
    )
    completion_cursor = date(2026, 7, 1)
    for ordinal, event in enumerate(completed_events, start=1):
        day = completion_cursor
        completion_cursor += timedelta(days=1)
        completion = "was completed" if ordinal % 2 else "concluded"
        cases.append(
            _case(
                f"positive-completion-{ordinal:02d}",
                f"recorder{ordinal:02d}@{PUBLIC_DOMAIN}",
                event,
                f"The {event} {completion} on {_surface(day)}; this is the final status.",
                members=(
                    _member(
                        event,
                        "unspecified",
                        "completed",
                        value=_value(day),
                        verdict="supported",
                        canonical=True,
                    ),
                ),
            )
        )

    alternative_events = (
        "Garnet Circle offsite",
        "Harbor Moss project debrief",
        "Ivory Peak interview",
        "Juniper Sound workshop",
        "Kingfisher Road planning meeting",
        "Lotus Bend interview",
    )
    for ordinal, event in enumerate(alternative_events, start=1):
        first = cursor
        second = cursor + timedelta(days=1)
        cursor += timedelta(days=2)
        cases.append(
            _case(
                f"positive-alternatives-{ordinal:02d}",
                f"organizer{ordinal:02d}@{PUBLIC_DOMAIN}",
                event,
                (
                    f"The {event} may happen on {_surface(first)} or "
                    f"{_surface(second)}. I will confirm the final choice tomorrow."
                ),
                members=(
                    _member(
                        event,
                        "occurrence",
                        "none",
                        values=(_value(first), _value(second)),
                        verdict="uncertain",
                        canonical=True,
                    ),
                    _member(
                        "confirm",
                        "unspecified",
                        "none",
                        value="2026-07-21",
                        verdict="supported",
                    ),
                ),
                forbidden=(
                    _forbidden(
                        event,
                        "occurrence",
                        "none",
                        "2026-07-21",
                    ),
                ),
                complete_group_required=True,
            )
        )

    dense_events = (
        ("Mica Valley interview", "Nectar Bay workshop", "send"),
        ("Osprey Lane briefing", "Pineglass product demo", "submit"),
        ("Quiet Harbor review", "Riverstone partner call", "reply"),
        ("Saffron Hill kickoff", "Timber Cove planning session", "file"),
        ("Ultramarine Forum", "Violet Marsh seminar", "complete"),
        ("Wildflower Council meeting", "Yellow Birch workshop", "respond"),
    )
    dense_objects = (
        "decision memo",
        "risk questionnaire",
        "attendance note",
        "registration form",
        "preparation checklist",
        "availability response",
    )
    for ordinal, ((first_event, second_event, action), object_name) in enumerate(
        zip(dense_events, dense_objects, strict=True),
        start=1,
    ):
        first = cursor
        second = cursor + timedelta(days=1)
        deadline = cursor + timedelta(days=2)
        cursor += timedelta(days=3)
        cases.append(
            _case(
                f"positive-dense-{ordinal:02d}",
                f"program{ordinal:02d}@{PUBLIC_DOMAIN}",
                first_event,
                (
                    f"The {first_event} is scheduled for {_surface(first)}. "
                    f"Separately, the {second_event} is scheduled for {_surface(second)}. "
                    f"Please {action} the {object_name} by {_surface(deadline)}."
                ),
                members=(
                    _member(
                        first_event,
                        "occurrence",
                        "scheduled",
                        value=_value(first),
                        verdict="supported",
                        canonical=True,
                    ),
                    _member(
                        second_event,
                        "occurrence",
                        "scheduled",
                        value=_value(second),
                        verdict="supported",
                        canonical=True,
                    ),
                    _member(
                        action,
                        "deadline",
                        "none",
                        value=_value(deadline),
                        verdict="supported",
                    ),
                ),
                forbidden=(
                    _forbidden(
                        first_event,
                        "occurrence",
                        "scheduled",
                        _value(second),
                    ),
                    _forbidden(
                        second_event,
                        "occurrence",
                        "scheduled",
                        _value(first),
                    ),
                    _forbidden(action, "deadline", "none", _value(first)),
                ),
            )
        )

    deadlines = (
        ("submit", "Orchid Path fellowship packet"),
        ("send", "Pebble Shore board summary"),
        ("file", "Quarry Lake compliance form"),
        ("complete", "Rosewood grant questionnaire"),
        ("reply", "Stonecrop partner survey"),
        ("register", "Tamarack mentor program"),
    )
    for ordinal, (action, object_name) in enumerate(deadlines, start=1):
        day = cursor
        cursor += timedelta(days=1)
        cases.append(
            _case(
                f"positive-deadline-{ordinal:02d}",
                f"operations{ordinal:02d}@{PUBLIC_DOMAIN}",
                f"Action date for {object_name}",
                f"Please {action} the {object_name} no later than {_surface(day)}.",
                members=(
                    _member(
                        action,
                        "deadline",
                        "none",
                        value=_value(day),
                        verdict="supported",
                    ),
                ),
            )
        )

    bridge_events = (
        "Upland Grove Leadership Forum",
        "Velvet Creek Research Conference",
        "Westwind Commons Design Summit",
        "Yucca Ridge Partner Conference",
    )
    for ordinal, event in enumerate(bridge_events, start=1):
        day = cursor
        cursor += timedelta(days=1)
        cases.append(
            _case(
                f"positive-subject-bridge-{ordinal:02d}",
                f"events{ordinal:02d}@{PUBLIC_DOMAIN}",
                event,
                f"When: {_surface(day)}\nVenue: Synthetic Hall {ordinal}.",
                members=(
                    _member(
                        event,
                        "occurrence",
                        "none",
                        value=_value(day),
                        verdict="supported",
                        canonical=True,
                    ),
                ),
            )
        )

    effective_policies = (
        "Zeeland Studio travel policy",
        "Acorn Harbor data-retention policy",
    )
    for ordinal, policy in enumerate(effective_policies, start=1):
        day = cursor
        cursor += timedelta(days=1)
        cases.append(
            _case(
                f"positive-effective-{ordinal:02d}",
                f"policy{ordinal:02d}@{PUBLIC_DOMAIN}",
                f"Effective date for {policy}",
                f"The {policy} becomes effective {_surface(day)}.",
                members=(
                    _member(
                        "becomes effective",
                        "occurrence",
                        "none",
                        value=_value(day),
                        verdict="supported",
                    ),
                ),
            )
        )

    windows = (
        "Birchlight Residency registration",
        "Cloudberry Fellows applications",
    )
    for ordinal, window in enumerate(windows, start=1):
        opening = cursor
        closing = cursor + timedelta(days=1)
        cursor += timedelta(days=2)
        opening_predicate = "open" if window.endswith("applications") else "opens"
        cases.append(
            _case(
                f"positive-open-close-{ordinal:02d}",
                f"enrollment{ordinal:02d}@{PUBLIC_DOMAIN}",
                f"Opening and closing dates for {window}",
                (
                    f"{window} {opening_predicate} {_surface(opening)} "
                    f"and closes {_surface(closing)}."
                ),
                members=(
                    _member(
                        window,
                        "occurrence",
                        "none",
                        value=_value(opening),
                        verdict="supported",
                    ),
                    _member(
                        "closes",
                        "deadline",
                        "none",
                        value=_value(closing),
                        verdict="supported",
                    ),
                ),
            )
        )

    cases.append(
        _case(
            "positive-relative-event-01",
            f"calendar@{PUBLIC_DOMAIN}",
            "Dogwood Terrace portfolio review",
            "The Dogwood Terrace portfolio review is scheduled for tomorrow.",
            members=(
                _member(
                    "Dogwood Terrace portfolio review",
                    "occurrence",
                    "scheduled",
                    value="2026-07-21",
                    verdict="supported",
                    canonical=True,
                ),
            ),
        )
    )
    cases.append(
        _case(
            "positive-relative-deadline-01",
            f"team@{PUBLIC_DOMAIN}",
            "Notes needed this week",
            "Please send the Eastbank interview notes by this coming Thursday.",
            members=(
                _member(
                    "send",
                    "deadline",
                    "none",
                    value="2026-07-23",
                    verdict="supported",
                ),
            ),
        )
    )
    return cases


def _negative_cases() -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    cursor = date(2026, 11, 1)

    negated_events = (
        "Ashen Brook interview",
        "Bramble Key workshop",
        "Cinder Vale planning meeting",
        "Dappled Shore demo",
        "Emberglass partner call",
        "Frost Pine review",
        "Ginkgo Harbor meeting",
        "Heather Run offsite",
    )
    for ordinal, event in enumerate(negated_events, start=1):
        day = cursor
        cursor += timedelta(days=1)
        cases.append(
            _case(
                f"{HARD_NEGATIVE_PREFIX}negation-{ordinal:02d}",
                f"discussion{ordinal:02d}@{PUBLIC_DOMAIN}",
                f"No confirmed timing for {event}",
                (
                    f"The {event} is not scheduled for {_surface(day)}. "
                    "That date appeared only in a discarded draft."
                ),
            )
        )

    hypothetical_events = (
        "Indigo Fen seminar",
        "Jetty Moss appointment",
        "Kelp Ridge conference",
        "Lilac Basin offsite",
        "Moonstone Field review",
        "Nettle Creek interview",
    )
    for ordinal, event in enumerate(hypothetical_events, start=1):
        day = cursor
        cursor += timedelta(days=1)
        if ordinal % 2:
            body = (
                f"If the {event} is scheduled for {_surface(day)}, would you attend? "
                "This is a scenario, not a booking."
            )
        else:
            body = (
                f"Could the {event} be scheduled for {_surface(day)}? "
                "No date has been selected."
            )
        cases.append(
            _case(
                f"{HARD_NEGATIVE_PREFIX}hypothetical-{ordinal:02d}",
                f"brainstorm{ordinal:02d}@{PUBLIC_DOMAIN}",
                f"Question about {event}",
                body,
            )
        )

    quoted_events = (
        "Oatgrass Lane interview",
        "Parchment Bay workshop",
        "Quill Harbor meeting",
        "Rainshadow Grove demo",
        "Sorrel Point appointment",
        "Topaz Creek meeting",
    )
    for ordinal, event in enumerate(quoted_events, start=1):
        day = cursor
        cursor += timedelta(days=1)
        quote_header = (
            "> Archived note:"
            if ordinal % 2
            else "---------- Forwarded history ----------"
        )
        cases.append(
            _case(
                f"{HARD_NEGATIVE_PREFIX}quoted-history-{ordinal:02d}",
                f"archive{ordinal:02d}@{PUBLIC_DOMAIN}",
                f"Current status of {event}",
                (
                    "There is no active date for this item.\n\n"
                    f"{quote_header}\nThe {event} was scheduled for {_surface(day)}."
                ),
            )
        )

    transactions = (
        ("package", "is expected to arrive"),
        ("replacement card", "is scheduled for delivery"),
        ("billing statement", "is due"),
        ("automated backup", "is scheduled for"),
        ("software subscription", "is due to renew"),
        ("service invoice", "was issued on"),
    )
    for ordinal, (item, phrase) in enumerate(transactions, start=1):
        day = cursor
        cursor += timedelta(days=1)
        cases.append(
            _case(
                f"{HARD_NEGATIVE_PREFIX}transaction-{ordinal:02d}",
                f"notifications{ordinal:02d}@{PUBLIC_DOMAIN}",
                f"Automated {item} notice",
                (
                    f"Your synthetic {item} {phrase} {_surface(day)}. "
                    "This automated receipt requires no action."
                ),
                labels=("CATEGORY_UPDATES",),
            )
        )

    promotions = (
        "analytics webinar",
        "creator masterclass",
        "growth conference",
        "product showcase",
        "sales workshop",
        "sponsored leadership call",
    )
    for ordinal, event in enumerate(promotions, start=1):
        day = cursor
        cursor += timedelta(days=1)
        cases.append(
            _case(
                f"{HARD_NEGATIVE_PREFIX}promotion-{ordinal:02d}",
                f"offers{ordinal:02d}@{PUBLIC_DOMAIN}",
                f"Sponsored invitation {ordinal:02d}",
                (
                    f"Advertisement: our free {event} is scheduled for {_surface(day)}. "
                    "Reserve a seat, save today, or unsubscribe."
                ),
                labels=("CATEGORY_PROMOTIONS",),
            )
        )

    wrong_scope = (
        ("hotel reservation", "was cancelled"),
        ("trial subscription", "was cancelled"),
        ("courier pickup", "was completed"),
        ("automated data export", "was completed"),
    )
    for ordinal, (item, lifecycle) in enumerate(wrong_scope, start=1):
        day = cursor
        cursor += timedelta(days=1)
        cases.append(
            _case(
                f"negative-wrong-scope-{ordinal:02d}",
                f"system{ordinal:02d}@{PUBLIC_DOMAIN}",
                "Routine account status",
                (
                    f"The synthetic {item} dated {_surface(day)} {lifecycle}. "
                    "This is routine account metadata, not a personal event."
                ),
                labels=("CATEGORY_UPDATES",),
            )
        )

    metadata_noise = (
        "This digest was generated on {day}; it contains no appointment or task.",
        "Copyright 2026 Synthetic Press. The footer was revised on {day}.",
        "Reference catalog 4407 was indexed on {day}; no response is needed.",
        "The weekly metrics snapshot covers data through {day} and is informational only.",
    )
    for ordinal, template in enumerate(metadata_noise, start=1):
        day = cursor
        cursor += timedelta(days=1)
        cases.append(
            _case(
                f"negative-metadata-noise-{ordinal:02d}",
                f"digest{ordinal:02d}@{PUBLIC_DOMAIN}",
                f"Synthetic informational digest {ordinal:02d}",
                template.format(day=_surface(day)),
                labels=("CATEGORY_UPDATES",),
            )
        )
    return cases


def _validate_variant(variant: int) -> None:
    if (
        isinstance(variant, bool)
        or not isinstance(variant, int)
        or not 1 <= variant <= 999
    ):
        raise PublicScaleFixtureError("public fixture variant is invalid")


def _challenge_id(variant: int) -> str:
    return CHALLENGE_ID if variant == 1 else f"{CHALLENGE_ID}-v{variant:03d}"


def _shift_dates(value: str, *, days: int) -> str:
    def shift_iso(match: re.Match[str]) -> str:
        return (date.fromisoformat(match.group(0)) + timedelta(days=days)).isoformat()

    def shift_surface(match: re.Match[str]) -> str:
        parsed = datetime.strptime(match.group(0), "%B %d, %Y").date()
        return _surface(parsed + timedelta(days=days))

    return _ISO_DATE_IN_TEXT_RE.sub(
        shift_iso,
        _TEXT_DATE_RE.sub(shift_surface, value),
    )


def _variant_paraphrases(variant: int) -> tuple[tuple[str, str], ...]:
    choices = (
        (
            (
                "The organizer's latest note confirms this timing.",
                "The latest organizer note confirms this slot.",
            ),
            ("Please use the replacement date.", "The newer date is current."),
            ("Separately, the ", "In a separate item, the "),
            (
                "That date appeared only in a discarded draft.",
                "It is merely a discarded draft date.",
            ),
            ("Please send the Eastbank", "Kindly send the Eastbank"),
        ),
        (
            (
                "The organizer's latest note confirms this timing.",
                "The confirmed calendar entry uses this timing.",
            ),
            ("Please use the replacement date.", "Only the later date should be used."),
            ("Separately, the ", "On another line, the "),
            (
                "That date appeared only in a discarded draft.",
                "The date belongs only to an abandoned draft.",
            ),
            ("Please send the Eastbank", "Send the Eastbank"),
        ),
        (
            (
                "The organizer's latest note confirms this timing.",
                "The organizer has reconfirmed this slot.",
            ),
            ("Please use the replacement date.", "The replacement date supersedes it."),
            ("Separately, the ", "Independently, the "),
            (
                "That date appeared only in a discarded draft.",
                "That date is draft-only and inactive.",
            ),
            ("Please send the Eastbank", "Remember to send the Eastbank"),
        ),
    )
    return choices[(variant - 2) % len(choices)]


def _variant_fixture(fixture: dict[str, Any], *, variant: int) -> dict[str, Any]:
    if variant == 1:
        return fixture
    marker = f"{_VARIANT_MARKERS[(variant - 2) % len(_VARIANT_MARKERS)]} {variant:03d}"
    names = sorted(
        {
            str(member["subject"])
            for row in fixture["cases"]
            for member in row["members"]
            if member.get("canonical_subject_required") is True
        },
        key=len,
        reverse=True,
    )
    replacements = tuple((name, f"{marker} {name}") for name in names)
    paraphrases = _variant_paraphrases(variant)
    # Later benchmark variants must still have been frozen before their model
    # predictions. Shift the synthetic source clock backward by whole weeks
    # instead of creating a future-dated authority while preserving weekdays.
    shift_days = -364 * (variant - 1)

    def transform_text(value: str) -> str:
        output = value
        for old, new in replacements:
            output = output.replace(old, new)
        for old, new in paraphrases:
            output = output.replace(old, new)
        return _shift_dates(output, days=shift_days)

    def transform(value: Any) -> Any:
        if isinstance(value, str):
            return transform_text(value)
        if isinstance(value, list):
            return [transform(item) for item in value]
        if isinstance(value, dict):
            return {key: transform(item) for key, item in value.items()}
        return value

    output = transform(fixture)
    output["challenge_id"] = _challenge_id(variant)
    output["account_email"] = f"owner-v{variant:03d}@{PUBLIC_DOMAIN}"
    for row in output["cases"]:
        case_id = str(row["case_id"])
        if case_id.startswith(HARD_NEGATIVE_PREFIX):
            row["case_id"] = (
                f"{HARD_NEGATIVE_PREFIX}v{variant:03d}-"
                f"{case_id.removeprefix(HARD_NEGATIVE_PREFIX)}"
            )
        else:
            row["case_id"] = f"v{variant:03d}-{case_id}"
        local, domain = str(row["sender"]).split("@", 1)
        row["sender"] = f"v{variant:03d}-{local}@{domain}"
    return output


def build_fixture(variant: int = 1) -> dict[str, Any]:
    """Return the deterministic public-only fixture value."""

    _validate_variant(variant)
    fixture = {
        "version": FIXTURE_VERSION,
        "challenge_id": _challenge_id(1),
        "created_at": CREATED_AT,
        "message_internal_at": MESSAGE_INTERNAL_AT,
        "account_email": ACCOUNT_EMAIL,
        "public_synthetic": True,
        "contains_private_gmail": False,
        "release_eligible": False,
        "cases": [*_positive_cases(), *_negative_cases()],
    }
    fixture = _variant_fixture(fixture, variant=variant)
    _validate_fixture(fixture, variant=variant)
    return fixture


def _fixture_counts(fixture: Mapping[str, Any]) -> dict[str, int]:
    cases = fixture["cases"]
    members = [member for row in cases for member in row["members"]]
    return {
        "cases": len(cases),
        "positive_cases": sum(bool(row["members"]) for row in cases),
        "negative_cases": sum(not row["members"] for row in cases),
        "gold_members": len(members),
        "supported_gold_members": sum(
            member["expected_verdict"] == "supported" for member in members
        ),
        "uncertain_gold_members": sum(
            member["expected_verdict"] == "uncertain" for member in members
        ),
        "canonical_subject_members": sum(
            member.get("canonical_subject_required") is True for member in members
        ),
        "complete_group_cases": sum(
            row["complete_group_required"] is True for row in cases
        ),
        "plausible_hard_negative_cases": sum(
            not row["members"] and str(row["case_id"]).startswith(HARD_NEGATIVE_PREFIX)
            for row in cases
        ),
        "structured_forbidden_bindings": sum(len(row["forbidden"]) for row in cases),
    }


def _validate_fixture(fixture: Mapping[str, Any], *, variant: int = 1) -> None:
    _validate_variant(variant)
    try:
        created_at = datetime.fromisoformat(str(fixture.get("created_at")))
        message_internal_at = datetime.fromisoformat(
            str(fixture.get("message_internal_at"))
        )
    except ValueError as exc:
        raise PublicScaleFixtureError("public fixture authority is invalid") from exc
    if (
        set(fixture) != _FIXTURE_KEYS
        or fixture.get("version") != FIXTURE_VERSION
        or fixture.get("challenge_id") != _challenge_id(variant)
        or fixture.get("public_synthetic") is not True
        or fixture.get("contains_private_gmail") is not False
        or fixture.get("release_eligible") is not False
        or created_at.tzinfo is None
        or message_internal_at.tzinfo is None
        or message_internal_at > created_at
        or not _PUBLIC_EMAIL_RE.fullmatch(str(fixture.get("account_email") or ""))
        or not isinstance(fixture.get("cases"), list)
    ):
        raise PublicScaleFixtureError("public fixture authority is invalid")

    seen: set[str] = set()
    for row in fixture["cases"]:
        if (
            not isinstance(row, Mapping)
            or set(row) != _CASE_KEYS
            or not isinstance(row.get("case_id"), str)
            or _CASE_ID_RE.fullmatch(row["case_id"]) is None
            or row["case_id"] in seen
            or not _PUBLIC_EMAIL_RE.fullmatch(str(row.get("sender") or ""))
            or not isinstance(row.get("subject"), str)
            or not row["subject"].strip()
            or "\x00" in row["subject"]
            or len(row["subject"]) > 500
            or not isinstance(row.get("body"), str)
            or not row["body"].strip()
            or "\x00" in row["body"]
            or len(row["body"]) > 20_000
            or not isinstance(row.get("label_ids"), list)
            or not row["label_ids"]
            or len(row["label_ids"]) != len(set(row["label_ids"]))
            or any(label not in _ALLOWED_LABEL_IDS for label in row["label_ids"])
            or not isinstance(row.get("members"), list)
            or not isinstance(row.get("forbidden"), list)
            or not isinstance(row.get("complete_group_required"), bool)
        ):
            raise PublicScaleFixtureError("public fixture case is invalid")
        seen.add(row["case_id"])
        for member in row["members"]:
            keys = set(member) if isinstance(member, Mapping) else set()
            has_value = "value" in keys
            has_values = "values" in keys
            values = member.get("values") if has_values else [member.get("value")]
            if (
                not isinstance(member, Mapping)
                or not {"subject", "relation", "lifecycle", "expected_verdict"} <= keys
                or not keys <= _MEMBER_KEYS
                or has_value == has_values
                or not isinstance(member.get("subject"), str)
                or not member["subject"].strip()
                or member.get("relation") not in _RELATIONS
                or member.get("lifecycle") not in _LIFECYCLES
                or member.get("expected_verdict") not in {"supported", "uncertain"}
                or not isinstance(values, list)
                or not values
                or len(values) != len(set(values))
                or any(
                    not isinstance(item, str) or _VALUE_RE.fullmatch(item) is None
                    for item in values
                )
                or (has_values and member.get("expected_verdict") != "uncertain")
                or (
                    "canonical_subject_required" in member
                    and member["canonical_subject_required"] is not True
                )
            ):
                raise PublicScaleFixtureError("public fixture member is invalid")
        for binding in row["forbidden"]:
            if (
                not isinstance(binding, Mapping)
                or set(binding) != {"subject", "relation", "lifecycle", "value"}
                or not isinstance(binding.get("subject"), str)
                or not binding["subject"].strip()
                or binding.get("relation") not in _RELATIONS
                or binding.get("lifecycle") not in _LIFECYCLES
                or not isinstance(binding.get("value"), str)
                or _VALUE_RE.fullmatch(binding["value"]) is None
            ):
                raise PublicScaleFixtureError("public fixture binding is invalid")

    counts = _fixture_counts(fixture)
    if counts != {
        "cases": 100,
        "positive_cases": 60,
        "negative_cases": 40,
        "gold_members": 88,
        "supported_gold_members": 82,
        "uncertain_gold_members": 6,
        "canonical_subject_members": 63,
        "complete_group_cases": 14,
        "plausible_hard_negative_cases": 32,
        "structured_forbidden_bindings": 46,
    }:
        raise PublicScaleFixtureError("public fixture aggregate contract is invalid")


def write_fixture(output_path: Path, *, variant: int = 1) -> dict[str, Any]:
    """Write one new canonical fixture with owner-only permissions."""

    fixture = build_fixture(variant)
    path = output_path.expanduser().absolute()
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(_canonical_json(fixture) + b"\n")
        except Exception:
            try:
                os.close(descriptor)
            except OSError:
                pass
            raise
        path.chmod(0o600)
    except (FileExistsError, OSError) as exc:
        raise PublicScaleFixtureError(
            "owner-only fixture could not be written"
        ) from exc
    return {
        "version": VERSION,
        "status": "complete",
        "variant": variant,
        **_fixture_counts(fixture),
        "external_calls": 0,
        "public_synthetic": True,
        "contains_private_gmail": False,
        "release_eligible": False,
        "source_content_printed": False,
    }


def _safe_failure() -> dict[str, Any]:
    return {
        "version": VERSION,
        "status": "failed",
        "error": "public_scale_fixture_build_failed",
        "external_calls": 0,
        "public_synthetic": True,
        "contains_private_gmail": False,
        "release_eligible": False,
        "source_content_printed": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--variant", type=int, default=1)
    args = parser.parse_args()
    try:
        result = write_fixture(args.output, variant=args.variant)
    except Exception:  # noqa: BLE001 - aggregate-only CLI boundary.
        print(json.dumps(_safe_failure(), sort_keys=True, separators=(",", ":")))
        raise SystemExit(2) from None
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    main()
