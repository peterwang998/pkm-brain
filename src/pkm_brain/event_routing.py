from __future__ import annotations

import re
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from .route_identity import enrich_page_identity_targets, guard_people_page_identity
from .temporal import canonical_event_bound, parse_temporal_value
from .util import slugify


EVENT_ROUTE_REVIEW_REASONS = {
    "event_temporal_identity_collision",
    "event_temporal_identity_multiple_occurrences",
    "event_temporal_identity_unresolved",
}
_EVENT_ROUTE_GUARD = "event_temporal_identity_v1"
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
_MONTH_PATTERN = (
    r"Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|"
    r"Nov(?:ember)?|Dec(?:ember)?"
)
_ISO_DATE_RANGE_RE = re.compile(
    r"\b(?P<start>\d{4}-\d{2}-\d{2})"
    r"(?:\s*(?:through|to|[-\u2013\u2014])\s*"
    r"(?P<end>\d{4}-\d{2}-\d{2}))?\b",
    re.IGNORECASE,
)
_MONTH_CROSS_RANGE_RE = re.compile(
    rf"\b(?P<start_month>{_MONTH_PATTERN})\.?\s+"
    r"(?P<start_day>\d{1,2})(?:st|nd|rd|th)?\s*"
    r"(?:through|to|[-\u2013\u2014])\s*"
    rf"(?P<end_month>{_MONTH_PATTERN})\.?\s+"
    r"(?P<end_day>\d{1,2})(?:st|nd|rd|th)?\s*,?\s*"
    r"(?P<year>\d{4})\b",
    re.IGNORECASE,
)
_MONTH_SAME_RANGE_RE = re.compile(
    rf"\b(?P<month>{_MONTH_PATTERN})\.?\s+"
    r"(?P<start_day>\d{1,2})(?:st|nd|rd|th)?\s*"
    r"(?:through|to|[-\u2013\u2014])\s*"
    r"(?P<end_day>\d{1,2})(?:st|nd|rd|th)?\s*,?\s*"
    r"(?P<year>\d{4})\b",
    re.IGNORECASE,
)
_MONTH_DATE_RE = re.compile(
    rf"\b(?P<month>{_MONTH_PATTERN})\.?\s+"
    r"(?P<day>\d{1,2})(?:st|nd|rd|th)?\s*,?\s*"
    r"(?P<year>\d{4})\b",
    re.IGNORECASE,
)
_NUMERIC_DATE_RE = re.compile(
    r"\b(?P<month>0?[1-9]|1[0-2])[/-]"
    r"(?P<day>0?[1-9]|[12]\d|3[01])[/-](?P<year>\d{4})\b"
)
_DATE_LIKE_RE = re.compile(
    rf"(?:\b\d{{4}}-\d{{2}}(?:-\d{{2}})?\b|"
    rf"\b(?:{_MONTH_PATTERN})\.?\s+\d{{1,2}}(?:st|nd|rd|th)?\b|"
    r"\b(?:0?[1-9]|1[0-2])/(?:0?[1-9]|[12]\d|3[01])(?:/\d{2,4})?\b)",
    re.IGNORECASE,
)
_OCCURRENCE_CUE_RE = re.compile(
    r"\b(?:occurred|happened|scheduled|rescheduled|cancelled|canceled|"
    r"starts?|started|begins?|began|ends?|ended|runs?|ran|takes?\s+place|"
    r"took\s+place|will\s+be\s+held|was\s+held|is\s+being\s+held|"
    r"attended|upcoming|deadline)\b",
    re.IGNORECASE,
)
_IDENTITY_SUFFIX_RE = re.compile(
    r"\s*\(\d{4}(?:-\d{2}(?:-\d{2})?)?(?:[T ][^)]*)?\)\s*$"
)

EventInterval = tuple[str, str]


def enrich_event_route_targets(
    conn: Any, route_targets: dict[str, dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    """Attach private occurrence evidence without exposing it to routing prompts."""

    enriched = enrich_page_identity_targets(conn, route_targets)
    intervals_by_page: dict[str, list[EventInterval]] = {}
    bounds_by_page: dict[str, list[tuple[str, str]]] = {}
    for row in conn.execute(
        """
        SELECT f.page_hint, f.statement, f.event_start_at, f.event_end_at,
               e.entity_type
        FROM facts f
        LEFT JOIN entities e ON e.id = f.entity_id
        WHERE f.status IN ('active', 'contested')
          AND f.page_hint IS NOT NULL
          AND (f.event_start_at IS NOT NULL OR e.entity_type = 'event')
        ORDER BY f.page_hint, f.created_at, f.id
        """
    ):
        page_hint = str(row["page_hint"] or "")
        if page_hint not in enriched:
            continue
        intervals = intervals_by_page.setdefault(page_hint, [])
        bound = interval_from_temporal_bounds(
            str(row["event_start_at"] or "") or None,
            str(row["event_end_at"] or "") or None,
        )
        if bound is not None:
            intervals.append(bound)
        canonical_start = canonical_event_bound(str(row["event_start_at"] or ""))
        canonical_end = canonical_event_bound(str(row["event_end_at"] or ""))
        if canonical_start:
            bounds_by_page.setdefault(page_hint, []).append(
                (canonical_start, canonical_end or "")
            )
        # Structured event bounds are half-open and therefore authoritative
        # when present.  Re-reading the statement as an inclusive date range
        # can otherwise add the exclusive end day back into the occurrence.
        if str(row["entity_type"] or "") == "event" and bound is None:
            intervals.extend(explicit_date_intervals(str(row["statement"] or "")))
    for page_hint, target in enriched.items():
        fact_intervals = collapse_intervals(intervals_by_page.get(page_hint, []))
        page_intervals = collapse_intervals(
            explicit_date_intervals(target_identity_text(target))
        )
        target["_event_fact_intervals"] = [list(item) for item in fact_intervals]
        target["_event_page_intervals"] = [list(item) for item in page_intervals]
        target["_event_occurrence_intervals"] = [
            list(item)
            for item in collapse_intervals([*fact_intervals, *page_intervals])
        ]
        target["_event_occurrence_bounds"] = [
            list(item) for item in sorted(set(bounds_by_page.get(page_hint, [])))
        ]
    return enriched


def event_route_targets_for_pages(
    conn: Any, page_hints: list[str]
) -> dict[str, dict[str, Any]]:
    targets: dict[str, dict[str, Any]] = {}
    for page_hint in dict.fromkeys(page for page in page_hints if page):
        contract = conn.execute(
            """
            SELECT canonical_entity, page_scope, retrieval_purpose
            FROM page_contracts
            WHERE page_hint = ? AND status = 'active'
            ORDER BY COALESCE(updated_at, created_at) DESC, id DESC
            LIMIT 1
            """,
            (page_hint,),
        ).fetchone()
        fact_exists = conn.execute(
            """
            SELECT 1 FROM facts
            WHERE page_hint = ? AND status IN ('active', 'contested')
            LIMIT 1
            """,
            (page_hint,),
        ).fetchone()
        if contract is None and fact_exists is None:
            continue
        targets[page_hint] = {
            "page_hint": page_hint,
            "canonical_entity": contract["canonical_entity"] if contract else None,
            "page_scope": contract["page_scope"] if contract else None,
            "retrieval_purpose": contract["retrieval_purpose"] if contract else None,
            "_route_target_exists": True,
        }
    return enrich_event_route_targets(conn, targets)


def guard_event_candidate_routes(
    candidates: list[dict[str, Any]],
    route_targets: dict[str, dict[str, Any]],
    *,
    accepted_anchor_candidates: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Guard routes and cohere only to explicitly accepted source anchors.

    A candidate in the current extraction batch is still only a proposal.  It
    must not lend occurrence identity to another fact that could survive after
    the anchor is rejected.  Callers may provide candidates loaded from an
    accepted action boundary (currently applied plus critic-agreed) separately.
    """

    guarded = [
        guard_event_candidate_route(candidate, route_targets)
        for candidate in candidates
    ]
    accepted_guarded = [
        guard_event_candidate_route(candidate, route_targets)
        for candidate in (accepted_anchor_candidates or [])
    ]
    anchors = [event_route_anchor(candidate) for candidate in accepted_guarded]
    anchors = [anchor for anchor in anchors if anchor is not None]
    output: list[dict[str, Any]] = []
    for candidate in guarded:
        if not candidate_routes_to_event_page(candidate):
            output.append(candidate)
            continue
        routing = candidate_routing(candidate)
        route_identity = candidate_metadata(candidate).get(
            "event_route_temporal_identity"
        )
        if (
            routing.get("route_destination_valid") is not False
            and isinstance(route_identity, dict)
            and (
                bool(candidate_event_intervals(candidate))
                or routing.get("route_resolution")
                != "event_page_occurrence_compatible"
            )
        ):
            output.append(candidate)
            continue
        matching = matching_event_route_anchors(candidate, anchors)
        identities = {
            (anchor["page_hint"], anchor["identity_token"])
            for anchor in matching
        }
        if len(identities) == 1:
            output.append(cohere_candidate_to_event_anchor(candidate, matching[0]))
        elif len(identities) > 1:
            output.append(
                held_event_route(
                    candidate, "event_temporal_identity_multiple_occurrences"
                )
            )
        else:
            output.append(candidate)
    return output


def guard_event_candidate_route(
    candidate: dict[str, Any],
    route_targets: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    candidate = guard_people_page_identity(candidate, route_targets)
    if candidate_routing(candidate).get("route_destination_valid") is False:
        return candidate
    primary = primary_named_event_mention(candidate)
    if primary is None:
        return guard_event_page_attachment(candidate, route_targets)
    if not candidate_is_occurrence_bearing(candidate):
        return guard_event_page_attachment(candidate, route_targets)
    return guard_primary_event_occurrence(candidate, route_targets)


def guard_primary_event_occurrence(
    candidate: dict[str, Any],
    route_targets: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Keep a primary named event on its deterministic occurrence page."""

    if primary_named_event_mention(candidate) is None:
        return candidate
    intervals = candidate_event_intervals(candidate)
    if not intervals:
        return held_event_route(candidate, "event_temporal_identity_unresolved")
    if not intervals_form_one_occurrence(intervals):
        return held_event_route(
            candidate, "event_temporal_identity_multiple_occurrences"
        )

    identity_token = event_identity_token(candidate, intervals)
    current_page = str(candidate.get("page_hint") or "")
    target = route_targets.get(current_page)
    target_intervals = target_occurrence_intervals(target)
    candidate_bounds = candidate_event_bounds(candidate)
    target_bounds = target_occurrence_bounds(target)
    current_page_intervals = explicit_date_intervals(target_identity_text(target))
    deterministic_page = deterministic_event_page(candidate, identity_token)
    exact_identity_involved = any(
        "T" in start for start, _end in [*candidate_bounds, *target_bounds]
    )
    current_target_conflict = bool(
        target_bounds
        and candidate_bounds
        and not event_bounds_compatible(candidate_bounds, target_bounds)
    ) or bool(
        target_intervals
        and not target_bounds
        and not intervals_compatible(intervals, target_intervals)
    )
    current_is_compatible = not current_target_conflict and bool(
        current_page == deterministic_page
        or (
            candidate_bounds
            and target_bounds
            and event_bounds_compatible(candidate_bounds, target_bounds)
        )
        or (
            not exact_identity_involved
            and target_intervals
            and intervals_compatible(intervals, target_intervals)
        )
        or (
            not exact_identity_involved
            and current_page_intervals
            and intervals_compatible(intervals, current_page_intervals)
        )
        or path_carries_identity(
            current_page, identity_token, exact=exact_identity_involved
        )
    )
    if current_is_compatible:
        return with_event_occurrence_identity(
            candidate,
            page_hint=current_page,
            identity_token=identity_token,
            intervals=intervals,
            route_resolution="event_temporal_identity_compatible",
            route_target_exists=target is not None,
        )

    deterministic_target = route_targets.get(deterministic_page)
    deterministic_intervals = target_occurrence_intervals(deterministic_target)
    deterministic_bounds = target_occurrence_bounds(deterministic_target)
    collision = bool(
        deterministic_bounds
        and candidate_bounds
        and not event_bounds_compatible(candidate_bounds, deterministic_bounds)
    ) or bool(
        deterministic_intervals
        and not deterministic_bounds
        and not intervals_compatible(intervals, deterministic_intervals)
    )
    if collision:
        return held_event_route(candidate, "event_temporal_identity_collision")
    return with_event_occurrence_identity(
        candidate,
        page_hint=deterministic_page,
        identity_token=identity_token,
        intervals=intervals,
        route_resolution="event_temporal_identity_reroute",
        route_target_exists=deterministic_target is not None,
    )


def guard_event_page_attachment(
    candidate: dict[str, Any],
    route_targets: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Require any fact on an event page to resolve to one occurrence."""

    current_page = str(candidate.get("page_hint") or "")
    if not candidate_routes_to_event_page(candidate):
        return candidate
    intervals = candidate_event_intervals(candidate)
    if intervals and not intervals_form_one_occurrence(intervals):
        return held_event_route(
            candidate, "event_temporal_identity_multiple_occurrences"
        )
    target = route_targets.get(current_page)
    target_intervals = target_occurrence_intervals(target)
    target_bounds = target_occurrence_bounds(target)
    # A model-proposed dated path is routing output, not occurrence evidence.
    # Dates carried by a path become usable only after that target is known to
    # exist.  A direct candidate interval remains independently grounded.
    page_intervals = explicit_date_intervals(current_page) if target else []
    if len({start for start, _end in target_bounds}) > 1:
        return held_event_route(
            candidate, "event_temporal_identity_multiple_occurrences"
        )
    grounded_target = target_intervals or page_intervals
    if grounded_target and not intervals_form_one_occurrence(grounded_target):
        return held_event_route(
            candidate, "event_temporal_identity_multiple_occurrences"
        )
    # An existing event page is routing context, not proof that an undated
    # source claim belongs to that occurrence. Only direct candidate time can
    # pass here; the batch guard may later rescue the claim from an already
    # applied, critic-agreed same-source occurrence anchor.
    if not intervals:
        return held_event_route(candidate, "event_temporal_identity_unresolved")
    if intervals and grounded_target and not intervals_compatible(
        intervals, grounded_target
    ):
        return held_event_route(candidate, "event_temporal_identity_collision")
    grounded_occurrence = grounded_target or intervals
    if not grounded_occurrence:
        return held_event_route(candidate, "event_temporal_identity_unresolved")
    identity_token = (
        target_bounds[0][0]
        if target_bounds
        else grounded_occurrence[0][0]
    )
    return with_event_occurrence_identity(
        candidate,
        page_hint=current_page,
        identity_token=identity_token,
        intervals=grounded_occurrence,
        route_resolution=(
            "event_page_occurrence_compatible"
            if target
            else "event_candidate_occurrence_compatible"
        ),
        route_target_exists=target is not None,
    )


def assert_extraction_event_route_safe(conn: Any, fact: dict[str, Any]) -> None:
    """Defense in depth for action paths that bypass extraction decisions."""

    if not extraction_event_candidate(fact):
        return
    current_page = str(fact.get("page_hint") or "")
    first_targets = event_route_targets_for_pages(conn, [current_page])
    guarded = guard_event_candidate_route(fact, first_targets)
    guarded_page = str(guarded.get("page_hint") or "")
    if guarded_page and guarded_page != current_page:
        targets = event_route_targets_for_pages(conn, [current_page, guarded_page])
        guarded = guard_event_candidate_route(fact, targets)
        guarded_page = str(guarded.get("page_hint") or "")
    routing = candidate_routing(guarded)
    if (
        routing.get("route_destination_valid") is False
        or guarded_page != current_page
    ):
        if str(routing.get("route_review_reason") or "").startswith("person_page_"):
            raise ValueError(
                "fact route is not compatible with its target page identity"
            )
        raise ValueError(
            "event fact route is not temporally compatible with its occurrence identity"
        )


def extraction_event_candidate(candidate: dict[str, Any]) -> bool:
    metadata = candidate_metadata(candidate)
    return str(candidate.get("extraction_method") or "") in {
        "llm",
        "structured_metadata",
    } or str(metadata.get("source") or "") in {
        "source_to_facts_extraction",
        "structured_event_projection",
    }


def candidate_event_intervals(candidate: dict[str, Any]) -> list[EventInterval]:
    raw_event_time = candidate.get("event_time")
    is_gmail = (
        str(candidate_metadata(candidate).get("source_type") or "")
        == "gmail_thread"
    )
    start_at: str | None = None
    end_at: str | None = None
    expression: str | None = None
    if isinstance(raw_event_time, dict):
        start_at = str(raw_event_time.get("start_at") or "") or None
        end_at = str(raw_event_time.get("end_at") or "") or None
        expression = str(raw_event_time.get("expression") or "") or None
    elif not is_gmail:
        start_at = str(candidate.get("event_start_at") or "") or None
        end_at = str(candidate.get("event_end_at") or "") or None
        expression = str(candidate.get("event_time_expression") or "") or None
    if start_at or end_at:
        if extraction_event_candidate(candidate) and expression:
            evidence = str(candidate.get("evidence_quote") or "")
            if compact_whitespace(expression).casefold() not in compact_whitespace(
                evidence
            ).casefold():
                return []
        interval = interval_from_temporal_bounds(start_at, end_at)
        if interval is not None:
            return [interval]
    # Gmail has a stricter, cited temporal-repair boundary. If that boundary
    # declined to retain event_time, routing must not recreate occurrence time
    # from looser regexes over model-authored statement text.
    if is_gmail:
        return []
    statement_intervals = set(
        explicit_date_intervals(str(candidate.get("statement") or ""))
    )
    evidence_intervals = set(
        explicit_date_intervals(str(candidate.get("evidence_quote") or ""))
    )
    return collapse_intervals(sorted(statement_intervals & evidence_intervals))


def candidate_event_bounds(candidate: dict[str, Any]) -> list[tuple[str, str]]:
    raw_event_time = candidate.get("event_time")
    if isinstance(raw_event_time, dict):
        start = canonical_event_bound(str(raw_event_time.get("start_at") or ""))
        end = canonical_event_bound(str(raw_event_time.get("end_at") or ""))
    else:
        start = canonical_event_bound(str(candidate.get("event_start_at") or ""))
        end = canonical_event_bound(str(candidate.get("event_end_at") or ""))
    return [(start, end or "")] if start else []


def explicit_date_intervals(value: str) -> list[EventInterval]:
    text = str(value or "")
    intervals: list[EventInterval] = []
    for match in _ISO_DATE_RANGE_RE.finditer(text):
        interval = checked_date_interval(match.group("start"), match.group("end"))
        if interval is not None:
            intervals.append(interval)
    for match in _MONTH_CROSS_RANGE_RE.finditer(text):
        interval = checked_month_interval(
            int(match.group("year")),
            month_number(match.group("start_month")),
            int(match.group("start_day")),
            month_number(match.group("end_month")),
            int(match.group("end_day")),
        )
        if interval is not None:
            intervals.append(interval)
    for match in _MONTH_SAME_RANGE_RE.finditer(text):
        month = month_number(match.group("month"))
        interval = checked_month_interval(
            int(match.group("year")),
            month,
            int(match.group("start_day")),
            month,
            int(match.group("end_day")),
        )
        if interval is not None:
            intervals.append(interval)
    for match in _MONTH_DATE_RE.finditer(text):
        interval = checked_month_interval(
            int(match.group("year")),
            month_number(match.group("month")),
            int(match.group("day")),
            month_number(match.group("month")),
            int(match.group("day")),
        )
        if interval is not None:
            intervals.append(interval)
    for match in _NUMERIC_DATE_RE.finditer(text):
        interval = checked_month_interval(
            int(match.group("year")),
            int(match.group("month")),
            int(match.group("day")),
            int(match.group("month")),
            int(match.group("day")),
        )
        if interval is not None:
            intervals.append(interval)
    return collapse_intervals(intervals)


def interval_from_temporal_bounds(
    start_at: str | None, end_at: str | None
) -> EventInterval | None:
    anchor = start_at or end_at
    if not anchor:
        return None
    start = parse_temporal_value(anchor, boundary="start")
    if start is None:
        return None
    if start_at and end_at:
        # event_time uses [start_at, end_at).  Convert that half-open clock to
        # the inclusive date envelopes used only by route compatibility.  In
        # particular, a next-day-exclusive end must not overlap an event that
        # starts on that adjacent day.
        exclusive_end = parse_temporal_value(end_at, boundary="start")
        if exclusive_end is None or exclusive_end <= start:
            return None
        end = exclusive_end - timedelta(microseconds=1)
    else:
        end = parse_temporal_value(anchor, boundary="end")
    if end is None or end < start:
        return None
    return start.date().isoformat(), end.date().isoformat()


def candidate_is_occurrence_bearing(candidate: dict[str, Any]) -> bool:
    if candidate.get("event_time") is not None or candidate.get("event_start_at"):
        return True
    text = "\n".join(
        (
            str(candidate.get("statement") or ""),
            str(candidate.get("evidence_quote") or ""),
        )
    )
    return bool(_DATE_LIKE_RE.search(text) or _OCCURRENCE_CUE_RE.search(text))


def candidate_routes_to_event_page(candidate: dict[str, Any]) -> bool:
    page_hint = str(candidate.get("page_hint") or "").strip().casefold()
    return page_hint == "events" or page_hint.startswith("events/")


def primary_named_event_mention(candidate: dict[str, Any]) -> dict[str, Any] | None:
    mentions = candidate.get("entity_mentions")
    if not isinstance(mentions, list):
        mentions = candidate_metadata(candidate).get("model_entity_mentions")
    if isinstance(mentions, list):
        for mention in mentions:
            if not isinstance(mention, dict) or not mention.get("is_primary"):
                continue
            entity_type = str(
                mention.get("entity_type") or mention.get("type") or ""
            ).strip().lower()
            mention_kind = str(mention.get("mention_kind") or "named").strip().lower()
            if entity_type == "event" and mention_kind == "named":
                return mention
    if str(candidate.get("entity_type") or "").strip().lower() != "event":
        return None
    surface = str(
        candidate.get("entity_mention")
        or candidate_metadata(candidate).get("model_entity_key")
        or ""
    ).strip()
    return {"surface": surface, "entity_type": "event", "is_primary": True} if surface else None


def deterministic_event_page(candidate: dict[str, Any], identity_token: str) -> str:
    base = event_identity_base(candidate) or "event"
    return f"events/{structured_event_occurrence_slug(base, identity_token)}.md"


def event_identity_base(candidate: dict[str, Any]) -> str:
    primary = primary_named_event_mention(candidate) or {}
    surface = str(primary.get("surface") or "").strip()
    if not surface:
        surface = str(candidate_metadata(candidate).get("model_entity_key") or "").strip()
    if not surface:
        surface = (
            str(candidate.get("page_hint") or "event")
            .removesuffix(".md")
            .split("/")[-1]
            .replace("-", " ")
        )
    return _IDENTITY_SUFFIX_RE.sub("", surface).strip()


def event_identity_token(
    candidate: dict[str, Any], intervals: list[EventInterval]
) -> str:
    raw = candidate.get("event_time")
    if isinstance(raw, dict):
        value = str(raw.get("start_at") or raw.get("end_at") or "").strip()
        if value:
            return value.replace("Z", "+00:00")
    return intervals[0][0]


def with_event_occurrence_identity(
    candidate: dict[str, Any],
    *,
    page_hint: str,
    identity_token: str,
    intervals: list[EventInterval],
    route_resolution: str,
    route_target_exists: bool,
) -> dict[str, Any]:
    routed = dict(candidate)
    original_page = str(candidate.get("page_hint") or "")
    routed["page_hint"] = page_hint
    routed["entity_key"] = event_route_entity_key(
        page_hint, str(routed.get("section_hint") or "Summary")
    )
    if page_hint != original_page:
        routed.pop("entity_id", None)
    metadata = candidate_metadata(routed)
    routing = candidate_routing(routed)
    routing.update(
        {
            "normalized_page_hint": page_hint,
            "route_destination_valid": True,
            "route_target_exists": route_target_exists,
            "route_resolution": route_resolution,
            "event_temporal_identity_guard": _EVENT_ROUTE_GUARD,
            "event_temporal_identity_original_page_hint": original_page,
        }
    )
    routing.pop("route_review_reason", None)
    routing.pop("event_temporal_identity_guard_locked", None)
    metadata["routing"] = routing
    metadata["event_route_temporal_identity"] = {
        "guard": _EVENT_ROUTE_GUARD,
        "identity_token": identity_token,
        "intervals": [list(item) for item in intervals],
    }
    routed["metadata"] = metadata
    if primary_named_event_mention(routed) is not None:
        return with_primary_event_identity(routed, identity_token)
    return routed


def held_event_route(candidate: dict[str, Any], reason: str) -> dict[str, Any]:
    held = dict(candidate)
    metadata = candidate_metadata(held)
    routing = candidate_routing(held)
    routing.update(
        {
            "route_destination_valid": False,
            "route_resolution": "held_for_routing_review",
            "route_review_reason": reason,
            "event_temporal_identity_guard": _EVENT_ROUTE_GUARD,
            "event_temporal_identity_guard_locked": True,
        }
    )
    metadata["routing"] = routing
    held["metadata"] = metadata
    return held


def with_primary_event_identity(
    candidate: dict[str, Any], identity_token: str
) -> dict[str, Any]:
    routed = dict(candidate)
    identity = f"{event_identity_base(candidate)} ({identity_token})"
    mentions = routed.get("entity_mentions")
    if isinstance(mentions, list):
        updated_mentions = []
        for mention in mentions:
            updated = dict(mention) if isinstance(mention, dict) else mention
            if isinstance(updated, dict) and updated.get("is_primary"):
                updated["entity_identity"] = identity
            updated_mentions.append(updated)
        routed["entity_mentions"] = updated_mentions
    metadata = candidate_metadata(routed)
    model_mentions = metadata.get("model_entity_mentions")
    if isinstance(model_mentions, list):
        updated_model_mentions = []
        for mention in model_mentions:
            updated = dict(mention) if isinstance(mention, dict) else mention
            if isinstance(updated, dict) and updated.get("is_primary"):
                updated["entity_identity"] = identity
            updated_model_mentions.append(updated)
        metadata["model_entity_mentions"] = updated_model_mentions
    metadata["event_occurrence_entity_identity"] = identity
    routed["metadata"] = metadata
    return routed


def event_route_anchor(candidate: dict[str, Any]) -> dict[str, Any] | None:
    direct_intervals = candidate_event_intervals(candidate)
    intervals = list(direct_intervals)
    routing = candidate_routing(candidate)
    identity = candidate_metadata(candidate).get("event_route_temporal_identity")
    if not intervals and isinstance(identity, dict):
        intervals = event_identity_intervals(identity)
    if (
        not intervals
        or routing.get("route_destination_valid") is False
        or not isinstance(identity, dict)
    ):
        return None
    return {
        "base_key": compact_identity(event_identity_base(candidate)),
        "source_ids": candidate_source_ids(candidate),
        "page_hint": str(candidate.get("page_hint") or ""),
        "original_page_hint": str(
            routing.get("event_temporal_identity_original_page_hint")
            or routing.get("original_page_hint")
            or candidate.get("page_hint")
            or ""
        ),
        "identity_token": str(identity.get("identity_token") or intervals[0][0]),
        "intervals": intervals,
        "source_occurrence_anchor": bool(direct_intervals),
        "route_target_exists": bool(routing.get("route_target_exists")),
    }


def cohere_candidate_to_event_anchor(
    candidate: dict[str, Any], anchor: dict[str, Any]
) -> dict[str, Any]:
    return with_event_occurrence_identity(
        candidate,
        page_hint=str(anchor["page_hint"]),
        identity_token=str(anchor["identity_token"]),
        intervals=list(anchor["intervals"]),
        route_resolution="event_source_occurrence_coherence",
        route_target_exists=bool(anchor["route_target_exists"]),
    )


def matching_event_route_anchors(
    candidate: dict[str, Any], anchors: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    source_ids = candidate_source_ids(candidate)
    if not source_ids:
        return []
    current_page = str(candidate.get("page_hint") or "")
    routing = candidate_routing(candidate)
    original_page = str(
        routing.get("event_temporal_identity_original_page_hint")
        or routing.get("original_page_hint")
        or current_page
    )
    primary = primary_named_event_mention(candidate)
    base_key = compact_identity(event_identity_base(candidate)) if primary else ""
    intervals = candidate_event_intervals(candidate)
    output: list[dict[str, Any]] = []
    for anchor in anchors:
        if not anchor.get("source_occurrence_anchor"):
            continue
        if not source_ids & anchor["source_ids"]:
            continue
        pages_related = bool(
            {current_page, original_page}
            & {anchor["page_hint"], anchor["original_page_hint"]}
        )
        if primary is not None and base_key == anchor["base_key"]:
            pages_related = True
        if not pages_related:
            continue
        if intervals and not intervals_compatible(intervals, anchor["intervals"]):
            continue
        output.append(anchor)
    return output


def event_identity_intervals(identity: dict[str, Any]) -> list[EventInterval]:
    raw = identity.get("intervals")
    intervals: list[EventInterval] = []
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, (list, tuple)) and len(item) == 2:
                interval = checked_date_interval(str(item[0]), str(item[1]))
                if interval is not None:
                    intervals.append(interval)
    return collapse_intervals(intervals)


def target_occurrence_intervals(target: dict[str, Any] | None) -> list[EventInterval]:
    if not isinstance(target, dict):
        return []
    raw = target.get("_event_occurrence_intervals")
    intervals: list[EventInterval] = []
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, (list, tuple)) and len(item) == 2:
                checked = checked_date_interval(str(item[0]), str(item[1]))
                if checked is not None:
                    intervals.append(checked)
    return collapse_intervals(intervals)


def target_occurrence_bounds(
    target: dict[str, Any] | None,
) -> list[tuple[str, str]]:
    if not isinstance(target, dict):
        return []
    raw = target.get("_event_occurrence_bounds")
    bounds: list[tuple[str, str]] = []
    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, (list, tuple)) or len(item) != 2:
                continue
            start = canonical_event_bound(str(item[0] or ""))
            end = canonical_event_bound(str(item[1] or ""))
            if start:
                bounds.append((start, end or ""))
    return sorted(set(bounds))


def event_bounds_compatible(
    candidate: list[tuple[str, str]], existing: list[tuple[str, str]]
) -> bool:
    candidate_starts = {start for start, _end in candidate}
    existing_starts = {start for start, _end in existing}
    return bool(candidate_starts and candidate_starts == existing_starts)


def intervals_compatible(
    candidate: list[EventInterval], existing: list[EventInterval]
) -> bool:
    return bool(candidate and existing) and all(
        any(intervals_overlap(left, right) for left in candidate) for right in existing
    ) and all(any(intervals_overlap(left, right) for right in existing) for left in candidate)


def intervals_form_one_occurrence(intervals: list[EventInterval]) -> bool:
    ordered = collapse_intervals(intervals)
    if not ordered:
        return False
    prior_end = date.fromisoformat(ordered[0][1])
    for start_text, end_text in ordered[1:]:
        start = date.fromisoformat(start_text)
        # A true multi-day expression is already one interval. Separate
        # adjacent intervals usually came from distinct legacy facts, so they
        # cannot safely establish one occurrence merely because the dates
        # touch.
        if start > prior_end:
            return False
        prior_end = max(prior_end, date.fromisoformat(end_text))
    return True


def collapse_intervals(intervals: list[EventInterval]) -> list[EventInterval]:
    valid = sorted(set(intervals))
    output: list[EventInterval] = []
    for interval in valid:
        if any(contains_interval(existing, interval) for existing in valid if existing != interval):
            continue
        output.append(interval)
    return output


def intervals_overlap(left: EventInterval, right: EventInterval) -> bool:
    return left[0] <= right[1] and right[0] <= left[1]


def contains_interval(outer: EventInterval, inner: EventInterval) -> bool:
    return outer[0] <= inner[0] and outer[1] >= inner[1]


def checked_date_interval(start_text: str, end_text: str | None) -> EventInterval | None:
    try:
        start = date.fromisoformat(start_text)
        end = date.fromisoformat(end_text or start_text)
    except ValueError:
        return None
    return (start.isoformat(), end.isoformat()) if end >= start else None


def checked_month_interval(
    year: int, start_month: int, start_day: int, end_month: int, end_day: int
) -> EventInterval | None:
    try:
        start = date(year, start_month, start_day)
        end = date(year, end_month, end_day)
    except ValueError:
        return None
    return (start.isoformat(), end.isoformat()) if end >= start else None


def month_number(value: str) -> int:
    return _MONTHS[str(value or "").strip().rstrip(".").casefold()]


def path_carries_identity(
    page_hint: str, identity_token: str, *, exact: bool = False
) -> bool:
    if exact:
        return event_route_slug(identity_token) in str(page_hint).casefold()
    identity_dates = explicit_date_intervals(identity_token)
    page_dates = explicit_date_intervals(page_hint)
    return bool(identity_dates and page_dates and intervals_compatible(identity_dates, page_dates))


def target_identity_text(target: dict[str, Any] | None) -> str:
    if not isinstance(target, dict):
        return ""
    return "\n".join(
        str(target.get(key) or "")
        for key in ("page_hint", "canonical_entity", "page_scope")
    )


def candidate_metadata(candidate: dict[str, Any]) -> dict[str, Any]:
    raw = candidate.get("metadata")
    return dict(raw) if isinstance(raw, dict) else {}


def candidate_routing(candidate: dict[str, Any]) -> dict[str, Any]:
    raw = candidate_metadata(candidate).get("routing")
    return dict(raw) if isinstance(raw, dict) else {}


def candidate_source_ids(candidate: dict[str, Any]) -> set[str]:
    raw = candidate.get("source_ids")
    return {str(item) for item in raw if str(item or "").strip()} if isinstance(raw, list) else set()


def compact_identity(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())


def compact_whitespace(value: str) -> str:
    return " ".join(str(value or "").split())


def structured_event_occurrence_slug(title: str, start_at: str) -> str:
    start_slug = event_route_slug(start_at)
    title_slug = event_route_slug(title)
    title_budget = max(1, 120 - len(start_slug) - 1)
    return f"{title_slug[:title_budget].rstrip('-')}-{start_slug}"


def event_route_slug(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", str(value).casefold()).strip("-")
    return normalized[:120] or "event"


def event_route_entity_key(page_hint: str, section_hint: str) -> str:
    path = Path(page_hint)
    topic = path.parts[0] if len(path.parts) > 1 else "root"
    stem = path.with_suffix("").as_posix()
    return f"{slugify(topic)}:{slugify(stem)}:{slugify(section_hint or 'page')}"
