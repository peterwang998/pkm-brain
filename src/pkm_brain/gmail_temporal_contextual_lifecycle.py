from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Literal

from .gmail_temporal_assertions import assess_gmail_temporal_source_assertions
from .gmail_temporal_batching import plan_gmail_temporal_selector_batches
from .gmail_temporal_frontier import build_gmail_temporal_candidate_frontier
from .gmail_temporal_leads import (
    TemporalLeadAnalysis,
    TemporalMention,
)


EXPERIMENT_VERSION = "gmail_temporal_contextual_lifecycle_experiment_v1"

LifecycleRole = Literal["cancelled", "completed", "rescheduled"]
AnchorStatus = Literal["scheduled", "cancelled", "completed"]
AnchorVerification = Literal[
    "unverified",
    "source_bound_self_identity",
    "external_verified",
    "owner_verified",
]
ObservationResolution = Literal["supported", "uncertain"]
IdentityEvidence = Literal[
    "cue_subject",
    "body_explicit",
    "subject_fallback",
    "none",
]

_SUPPORTED_ROLES = frozenset({"cancelled", "completed", "rescheduled"})
# Owner authority remains closed until the production owner-receipt ledger exists.
_CROSS_MESSAGE_AUTHORITIES = frozenset({"external_verified"})
_ASSERTION_OMISSION_REASONS = {
    "quoted_or_forwarded_context": "quoted_or_forwarded_lifecycle_cue",
    "denied_assertion": "denied_lifecycle_cue",
    "negated_assertion": "negated_lifecycle_cue",
    "conditional_assertion": "conditional_lifecycle_cue",
    "interrogative_assertion": "interrogative_lifecycle_cue",
    "reported_assertion": "reported_lifecycle_cue",
    "refuted_assertion": "refuted_lifecycle_cue",
    "modal_assertion": "modal_lifecycle_cue",
    "epistemic_assertion": "epistemic_lifecycle_cue",
}
_OPAQUE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,511}$")
_AUXILIARY_SUFFIX = re.compile(
    r"\s+(?:was|were|is|are|got|has\s+been|have\s+been|had\s+been)\s*$",
    re.IGNORECASE,
)
_SUBJECT_LINE = re.compile(r"\ASubject:[ \t]*(?P<value>[^\r\n]*)", re.IGNORECASE)
_REPLY_PREFIX = re.compile(r"\A(?:(?:re|fw|fwd)[ \t]*:[ \t]*)+", re.IGNORECASE)
_LIFECYCLE_TITLE_PREFIX = re.compile(
    r"\A(?:cancelled|canceled|completed|finished|rescheduled|postponed)"
    r"[ \t]*:[ \t]*",
    re.IGNORECASE,
)
_TOKEN = re.compile(r"[A-Za-z0-9]+")
_IDENTITY_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "are",
        "been",
        "cancelled",
        "canceled",
        "complete",
        "completed",
        "done",
        "event",
        "finished",
        "for",
        "fw",
        "fwd",
        "has",
        "have",
        "is",
        "it",
        "my",
        "our",
        "postponed",
        "re",
        "rescheduled",
        "subject",
        "that",
        "the",
        "this",
        "update",
        "was",
        "were",
        "your",
    }
)
_RECENT_OBJECT_NOUNS = frozenset(
    {
        "account",
        "appointment",
        "booking",
        "call",
        "conference",
        "delivery",
        "dinner",
        "flight",
        "hotel",
        "interview",
        "invoice",
        "meeting",
        "membership",
        "order",
        "package",
        "reservation",
        "review",
        "session",
        "subscription",
        "sync",
        "workshop",
    }
)


class GmailTemporalContextualLifecycleError(ValueError):
    """Raised when experimental contextual-lifecycle authority is malformed."""


@dataclass(frozen=True)
class GmailTemporalPriorEventAnchor:
    """One trusted, already-known event eligible for contextual review only."""

    version: Literal["gmail_temporal_prior_event_anchor_v1"]
    event_identity_key: str
    gmail_thread_id: str
    source_message_id: str
    source_internal_at: str
    subject_aliases: tuple[str, ...]
    current_status: AnchorStatus
    identity_verification: AnchorVerification


@dataclass(frozen=True)
class GmailTemporalContextualLifecycleObservation:
    """A date-free lifecycle cue bound to prior event identity for review."""

    version: Literal["gmail_temporal_contextual_lifecycle_observation_v1"]
    observation_id: str
    mention_id: str
    lifecycle: LifecycleRole
    resolution: ObservationResolution
    selected_event_identity_key: str | None
    possible_event_identity_keys: tuple[str, ...]
    evidence_start: int
    evidence_end: int
    prior_anchor_snapshot_fingerprint: str
    reasons: tuple[str, ...]
    candidate_authorization: Literal[False] = False
    requires_defer: Literal[True] = True
    routable: Literal[False] = False


@dataclass(frozen=True)
class GmailTemporalContextualLifecycleOmission:
    version: Literal["gmail_temporal_contextual_lifecycle_omission_v1"]
    reason: str
    mention_ids: tuple[str, ...]


@dataclass(frozen=True)
class GmailTemporalContextualLifecyclePlan:
    """Complete disabled experiment result; it is not a production projection."""

    version: Literal["gmail_temporal_contextual_lifecycle_plan_v1"]
    experiment_version: Literal["gmail_temporal_contextual_lifecycle_experiment_v1"]
    plan_fingerprint: str
    gmail_thread_id: str
    gmail_message_id: str
    source_sha256: str
    analysis_fingerprint: str
    assertion_assessment_fingerprint: str
    eligible_prior_anchor_snapshot_fingerprint: str
    eligible_prior_anchors: tuple[GmailTemporalPriorEventAnchor, ...]
    observations: tuple[GmailTemporalContextualLifecycleObservation, ...]
    omissions: tuple[GmailTemporalContextualLifecycleOmission, ...]
    production_integration_enabled: Literal[False] = False
    complete: Literal[True] = True
    candidate_authorization: Literal[False] = False
    requires_defer: Literal[True] = True
    routable: Literal[False] = False


def plan_gmail_temporal_contextual_lifecycle_experiment(
    *,
    text: str,
    message_internal_at: str,
    fact_admitted: bool,
    temporal_review_rescue: bool,
    gmail_thread_id: str,
    gmail_message_id: str,
    prior_events: tuple[GmailTemporalPriorEventAnchor, ...],
) -> GmailTemporalContextualLifecyclePlan:
    """Plan a narrow date-free rescue without touching the production path.

    Only immutable existing event identities may be referenced.  A cue cannot
    create an event or occurrence, and every result remains deferred and
    non-routable.  Reschedules without a replacement endpoint are always
    uncertain.  Ambiguous identity never selects an event.
    """

    _require_opaque(gmail_thread_id, "gmail_thread_id")
    _require_opaque(gmail_message_id, "gmail_message_id")
    current_at = _aware_time(message_internal_at, "message_internal_at")
    if not isinstance(text, str):
        raise GmailTemporalContextualLifecycleError("text must be a string")
    if not isinstance(prior_events, tuple):
        raise GmailTemporalContextualLifecycleError("prior_events must be a tuple")

    chunk_id = f"contextual-lifecycle:{gmail_thread_id}:{gmail_message_id}"
    assertion_assessment = assess_gmail_temporal_source_assertions(
        text=text,
        message_internal_at=current_at,
        fact_admitted=fact_admitted,
        temporal_review_rescue=temporal_review_rescue,
        chunk_id=chunk_id,
    )
    analysis = assertion_assessment.analysis
    lifecycle_mentions = tuple(
        item
        for item in analysis.mentions
        if item.mention_type == "lifecycle" and item.lifecycle_role in _SUPPORTED_ROLES
    )
    omissions: list[GmailTemporalContextualLifecycleOmission] = []
    observations: list[GmailTemporalContextualLifecycleObservation] = []
    eligible = _eligible_anchors(
        prior_events,
        gmail_thread_id=gmail_thread_id,
        current_at=current_at,
    )
    prior_anchor_snapshot_fingerprint = _prior_anchor_snapshot_fingerprint(eligible)

    if not (fact_admitted or temporal_review_rescue):
        omissions.append(_omission("message_not_admitted", lifecycle_mentions))
    elif len(lifecycle_mentions) != 1:
        reason = (
            "no_contextual_lifecycle_cue"
            if not lifecycle_mentions
            else "multiple_contextual_lifecycle_cues"
        )
        omissions.append(_omission(reason, lifecycle_mentions))
    else:
        cue = lifecycle_mentions[0]
        cue_assertion = next(
            item
            for item in assertion_assessment.lifecycle_assertions
            if item.mention_id == cue.mention_id
        )
        cue_blocker = (
            _ASSERTION_OMISSION_REASONS.get(
                cue_assertion.primary_blocker,
                "source_assertion_blocked",
            )
            if cue_assertion.primary_blocker is not None
            else None
        )
        if cue_blocker is not None:
            omissions.append(_omission(cue_blocker, lifecycle_mentions))
        elif _cue_has_standard_association(text, analysis, cue):
            omissions.append(_omission("standard_temporal_path_present", (cue,)))
        else:
            if not eligible:
                omissions.append(_omission("no_verified_active_prior_event", (cue,)))
            else:
                observation, reason = _resolve_observation(
                    text=text,
                    analysis=analysis,
                    cue=cue,
                    eligible=eligible,
                    prior_anchor_snapshot_fingerprint=(
                        prior_anchor_snapshot_fingerprint
                    ),
                )
                if observation is None:
                    omissions.append(_omission(reason, (cue,)))
                else:
                    observations.append(observation)

    return _plan(
        gmail_thread_id=gmail_thread_id,
        gmail_message_id=gmail_message_id,
        analysis=analysis,
        assertion_assessment_fingerprint=(assertion_assessment.assessment_fingerprint),
        eligible_prior_anchors=eligible,
        prior_anchor_snapshot_fingerprint=prior_anchor_snapshot_fingerprint,
        observations=tuple(observations),
        omissions=tuple(omissions),
    )


def _eligible_anchors(
    anchors: tuple[GmailTemporalPriorEventAnchor, ...],
    *,
    gmail_thread_id: str,
    current_at: datetime,
) -> tuple[GmailTemporalPriorEventAnchor, ...]:
    output: list[GmailTemporalPriorEventAnchor] = []
    seen: set[str] = set()
    for anchor in anchors:
        if not isinstance(anchor, GmailTemporalPriorEventAnchor):
            raise GmailTemporalContextualLifecycleError("prior event anchor is invalid")
        _require_opaque(anchor.event_identity_key, "event_identity_key")
        _require_opaque(anchor.gmail_thread_id, "anchor gmail_thread_id")
        _require_opaque(anchor.source_message_id, "source_message_id")
        if (
            anchor.version != "gmail_temporal_prior_event_anchor_v1"
            or not isinstance(anchor.subject_aliases, tuple)
            or not anchor.subject_aliases
            or any(not _identity_tokens(value) for value in anchor.subject_aliases)
            or anchor.current_status not in {"scheduled", "cancelled", "completed"}
            or anchor.identity_verification
            not in {
                "unverified",
                "source_bound_self_identity",
                "external_verified",
                "owner_verified",
            }
        ):
            raise GmailTemporalContextualLifecycleError("prior event anchor is invalid")
        source_at = _aware_time(anchor.source_internal_at, "source_internal_at")
        if anchor.event_identity_key in seen:
            raise GmailTemporalContextualLifecycleError(
                "prior event identities must be unique"
            )
        seen.add(anchor.event_identity_key)
        if (
            anchor.gmail_thread_id == gmail_thread_id
            and source_at < current_at
            and anchor.current_status == "scheduled"
            and anchor.identity_verification in _CROSS_MESSAGE_AUTHORITIES
        ):
            output.append(anchor)
    return tuple(sorted(output, key=lambda item: item.event_identity_key))


def _cue_has_standard_association(
    text: str,
    analysis: TemporalLeadAnalysis,
    cue: TemporalMention,
) -> bool:
    """Return whether the ordinary pipeline covers this exact cue locality."""

    batch_plan = plan_gmail_temporal_selector_batches(text=text, analysis=analysis)
    expressions = {item.expression_id: item for item in analysis.expressions}
    return any(
        candidate.lifecycle_mention_id == cue.mention_id
        and expressions[candidate.expression_id].segment_id == cue.segment_id
        for batch in batch_plan.batches
        for candidate in build_gmail_temporal_candidate_frontier(
            analysis=analysis,
            batch=batch,
        ).candidates
    )


def _resolve_observation(
    *,
    text: str,
    analysis: TemporalLeadAnalysis,
    cue: TemporalMention,
    eligible: tuple[GmailTemporalPriorEventAnchor, ...],
    prior_anchor_snapshot_fingerprint: str,
) -> tuple[GmailTemporalContextualLifecycleObservation | None, str]:
    role = cue.lifecycle_role
    if role not in _SUPPORTED_ROLES:
        return None, "unsupported_lifecycle_role"
    identity_tokens, identity_evidence = _cue_identity_tokens(text, cue)
    exact_matches = tuple(
        anchor
        for anchor in eligible
        if identity_tokens
        and any(
            _tokens_exact(identity_tokens, _identity_tokens(alias))
            for alias in anchor.subject_aliases
        )
    )
    compatible_matches = tuple(
        anchor
        for anchor in eligible
        if identity_tokens
        and any(
            _tokens_subset_compatible(identity_tokens, _identity_tokens(alias))
            for alias in anchor.subject_aliases
        )
    )
    if (
        identity_evidence in {"cue_subject", "body_explicit", "subject_fallback"}
        and not compatible_matches
    ):
        return None, "explicit_event_identity_mismatch"
    if identity_evidence == "subject_fallback" and _recent_object_conflicts(
        text,
        cue,
        subject_tokens=identity_tokens,
    ):
        return None, "stale_subject_recent_object_conflict"

    possible = compatible_matches if compatible_matches else eligible
    keys = tuple(item.event_identity_key for item in possible)
    selected: str | None = None
    resolution: ObservationResolution = "uncertain"
    reasons: tuple[str, ...]
    if role == "rescheduled":
        reasons = ("replacement_endpoint_missing",)
    elif identity_evidence == "subject_fallback":
        reasons = (
            (
                "subject_only_identity_requires_review"
                if len(possible) == 1
                else "multiple_possible_thread_events"
            ),
        )
    elif len(exact_matches) == 1 and len(compatible_matches) == 1:
        selected = exact_matches[0].event_identity_key
        resolution = "supported"
        reasons = ("exact_subject_alias_match",)
    elif compatible_matches:
        reasons = (
            "subset_subject_alias_match_requires_review"
            if len(compatible_matches) == 1
            else "multiple_possible_thread_events",
        )
    elif identity_evidence == "none" and len(eligible) == 1:
        reasons = ("unique_thread_event_anaphora",)
    elif len(possible) > 1:
        reasons = ("multiple_possible_thread_events",)
    else:
        return None, "event_identity_unresolved"

    material = {
        "version": "gmail_temporal_contextual_lifecycle_observation_v1",
        "analysis_fingerprint": analysis.snapshot_fingerprint,
        "mention_id": cue.mention_id,
        "lifecycle": role,
        "resolution": resolution,
        "selected_event_identity_key": selected,
        "possible_event_identity_keys": keys,
        "evidence_start": cue.start,
        "evidence_end": cue.end,
        "prior_anchor_snapshot_fingerprint": prior_anchor_snapshot_fingerprint,
        "reasons": reasons,
    }
    return (
        GmailTemporalContextualLifecycleObservation(
            version="gmail_temporal_contextual_lifecycle_observation_v1",
            observation_id="gtclo_" + _digest(material),
            mention_id=cue.mention_id,
            lifecycle=role,
            resolution=resolution,
            selected_event_identity_key=selected,
            possible_event_identity_keys=keys,
            evidence_start=cue.start,
            evidence_end=cue.end,
            prior_anchor_snapshot_fingerprint=prior_anchor_snapshot_fingerprint,
            reasons=reasons,
        ),
        "",
    )


def _cue_identity_tokens(
    text: str,
    cue: TemporalMention,
) -> tuple[tuple[str, ...], IdentityEvidence]:
    subject_match = _SUBJECT_LINE.match(text)
    subject = subject_match.group("value") if subject_match is not None else ""
    normalized_subject = _REPLY_PREFIX.sub("", subject).strip()
    normalized_subject = _LIFECYCLE_TITLE_PREFIX.sub("", normalized_subject).strip()

    if cue.field == "subject":
        tokens = _identity_tokens(normalized_subject)
        return tokens, "cue_subject" if tokens else "none"

    clause_start = max(
        text.rfind("\n", 0, cue.start),
        text.rfind(".", 0, cue.start),
        text.rfind("!", 0, cue.start),
        text.rfind("?", 0, cue.start),
    )
    clause = text[clause_start + 1 : cue.start].strip()
    without_auxiliary = _AUXILIARY_SUFFIX.sub("", clause).strip()
    body_tokens = _identity_tokens(without_auxiliary)
    body_is_pronoun = not body_tokens
    if not body_is_pronoun:
        return body_tokens, "body_explicit"

    subject_tokens = _identity_tokens(normalized_subject)
    return subject_tokens, "subject_fallback" if subject_tokens else "none"


def _recent_object_conflicts(
    text: str,
    cue: TemporalMention,
    *,
    subject_tokens: tuple[str, ...],
) -> bool:
    prefix = text[: cue.start]
    clauses = tuple(
        item.strip()
        for item in re.split(r"[.!?\n]+", prefix)
        if item.strip() and not item.lstrip().lower().startswith("subject:")
    )
    if not clauses:
        return False
    tokenized = tuple(_identity_tokens(item) for item in clauses)
    recent_tokens = next((item for item in reversed(tokenized) if item), ())
    return bool(
        set(recent_tokens) & _RECENT_OBJECT_NOUNS
        and not _tokens_subset_compatible(recent_tokens, subject_tokens)
    )


def _identity_tokens(value: str) -> tuple[str, ...]:
    return tuple(
        token
        for token in (item.lower() for item in _TOKEN.findall(value))
        if token not in _IDENTITY_STOPWORDS
    )


def _tokens_exact(left: tuple[str, ...], right: tuple[str, ...]) -> bool:
    return bool(left and right and left == right)


def _tokens_subset_compatible(
    left: tuple[str, ...],
    right: tuple[str, ...],
) -> bool:
    if not left or not right:
        return False
    left_set = set(left)
    right_set = set(right)
    return left_set == right_set or (
        min(len(left_set), len(right_set)) >= 2
        and (left_set.issubset(right_set) or right_set.issubset(left_set))
    )


def _omission(
    reason: str,
    mentions: tuple[TemporalMention, ...],
) -> GmailTemporalContextualLifecycleOmission:
    return GmailTemporalContextualLifecycleOmission(
        version="gmail_temporal_contextual_lifecycle_omission_v1",
        reason=reason,
        mention_ids=tuple(item.mention_id for item in mentions),
    )


def _plan(
    *,
    gmail_thread_id: str,
    gmail_message_id: str,
    analysis: TemporalLeadAnalysis,
    assertion_assessment_fingerprint: str,
    eligible_prior_anchors: tuple[GmailTemporalPriorEventAnchor, ...],
    prior_anchor_snapshot_fingerprint: str,
    observations: tuple[GmailTemporalContextualLifecycleObservation, ...],
    omissions: tuple[GmailTemporalContextualLifecycleOmission, ...],
) -> GmailTemporalContextualLifecyclePlan:
    material = {
        "version": "gmail_temporal_contextual_lifecycle_plan_v1",
        "experiment_version": EXPERIMENT_VERSION,
        "gmail_thread_id": gmail_thread_id,
        "gmail_message_id": gmail_message_id,
        "source_sha256": analysis.source_sha256,
        "analysis_fingerprint": analysis.snapshot_fingerprint,
        "assertion_assessment_fingerprint": assertion_assessment_fingerprint,
        "eligible_prior_anchor_snapshot_fingerprint": (
            prior_anchor_snapshot_fingerprint
        ),
        "eligible_prior_anchors": [asdict(item) for item in eligible_prior_anchors],
        "observations": [asdict(item) for item in observations],
        "omissions": [asdict(item) for item in omissions],
        "production_integration_enabled": False,
        "complete": True,
        "candidate_authorization": False,
        "requires_defer": True,
        "routable": False,
    }
    return GmailTemporalContextualLifecyclePlan(
        version="gmail_temporal_contextual_lifecycle_plan_v1",
        experiment_version=EXPERIMENT_VERSION,
        plan_fingerprint="gtclp_" + _digest(material),
        gmail_thread_id=gmail_thread_id,
        gmail_message_id=gmail_message_id,
        source_sha256=analysis.source_sha256,
        analysis_fingerprint=analysis.snapshot_fingerprint,
        assertion_assessment_fingerprint=assertion_assessment_fingerprint,
        eligible_prior_anchor_snapshot_fingerprint=(prior_anchor_snapshot_fingerprint),
        eligible_prior_anchors=eligible_prior_anchors,
        observations=observations,
        omissions=omissions,
    )


def _aware_time(value: str, label: str) -> datetime:
    if not isinstance(value, str):
        raise GmailTemporalContextualLifecycleError(f"{label} must be an ISO time")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise GmailTemporalContextualLifecycleError(
            f"{label} must be an ISO time"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise GmailTemporalContextualLifecycleError(f"{label} must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _require_opaque(value: str, label: str) -> None:
    if not isinstance(value, str) or _OPAQUE.fullmatch(value) is None:
        raise GmailTemporalContextualLifecycleError(f"{label} is invalid")


def _prior_anchor_snapshot_fingerprint(
    anchors: tuple[GmailTemporalPriorEventAnchor, ...],
) -> str:
    return "gtclas_" + _digest(
        {
            "version": "gmail_temporal_contextual_lifecycle_anchor_snapshot_v1",
            "eligible_prior_anchors": [asdict(item) for item in anchors],
        }
    )


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
