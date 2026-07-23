from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import asdict, dataclass, field
from typing import Literal

from .gmail_sensitive_data import (
    GMAIL_SENSITIVE_DATA_VERSION,
    sanitize_gmail_model_payload,
)
from .gmail_temporal_event_identity import (
    GmailTemporalEventIdentityPlan,
    GmailTemporalEventIdentityResolution,
    plan_gmail_temporal_event_identity,
    validate_gmail_temporal_event_identity_resolution,
)
from .gmail_temporal_leads import TemporalLeadAnalysis, TemporalMention
from .gmail_temporal_review import gmail_temporal_review_grouping_policy_fingerprint
from .gmail_temporal_thread_lifecycle import (
    GmailTemporalThreadMessageReview,
    GmailTemporalThreadSnapshotAuthority,
)
from .gmail_temporal_thread_retrieval_experiment import (
    EXPERIMENT_VERSION,
    GMAIL_TEMPORAL_VERIFIED_EVENT_ALIAS_POLICY_VERSION,
    GmailTemporalVerifiedEventBinding,
    gmail_temporal_verified_event_alias_key,
)


GMAIL_TEMPORAL_EVENT_IDENTITY_CONSUMER_POLICY_VERSION = (
    "gmail_temporal_event_identity_consumer_policy_v1"
)
GMAIL_TEMPORAL_EVENT_IDENTITY_RETRIEVAL_BINDING_POLICY_VERSION = (
    "gmail_temporal_event_identity_retrieval_binding_policy_v1"
)
GMAIL_TEMPORAL_EVENT_IDENTITY_PAIR_REQUEST_VERSION = (
    "gmail_temporal_event_identity_pair_request_v1"
)

_SOURCE_TEXT_VERSION = "gmail_temporal_event_identity_source_text_v1"
_MENTION_SURFACE_VERSION = "gmail_temporal_event_identity_mention_surface_v1"
_UNIT_SURFACE_VERSION = "gmail_temporal_event_identity_unit_surface_v1"
_SURFACE_AUTHORITY_VERSION = "gmail_temporal_event_identity_surface_authority_v1"

MAX_EVENT_IDENTITY_SURFACE_CHARS = 512
MAX_EVENT_IDENTITY_ALIAS_SURFACES_PER_UNIT = 16
MAX_EVENT_IDENTITY_PAIR_REQUEST_BYTES = 512 * 1024

_TOKEN = re.compile(r"[^\W_]+", re.UNICODE)
_GENERIC_EVENT_NAME_TOKENS = frozenset(
    {
        "a",
        "an",
        "annual",
        "appointment",
        "architecture",
        "birthday",
        "board",
        "booking",
        "briefing",
        "budget",
        "call",
        "ceremony",
        "check",
        "checkin",
        "class",
        "client",
        "conference",
        "concert",
        "customer",
        "debrief",
        "delivery",
        "demo",
        "design",
        "dinner",
        "discussion",
        "event",
        "exam",
        "executive",
        "final",
        "finance",
        "flight",
        "forum",
        "hearing",
        "interview",
        "kickoff",
        "launch",
        "leadership",
        "meeting",
        "monthly",
        "offsite",
        "orientation",
        "partner",
        "party",
        "performance",
        "pickup",
        "planning",
        "presentation",
        "product",
        "project",
        "quarterly",
        "reservation",
        "review",
        "roadmap",
        "sales",
        "screening",
        "session",
        "status",
        "stay",
        "strategy",
        "summit",
        "sync",
        "team",
        "technical",
        "the",
        "tour",
        "training",
        "trip",
        "update",
        "visit",
        "webinar",
        "weekly",
        "workshop",
        "yearly",
    }
)

GMAIL_TEMPORAL_EVENT_IDENTITY_PAIR_CONTRACT = """Decide whether each pair refers to the same real-world event. Use verifier_selected_evidence only as evidence for the temporal claim. deterministic_identity_metadata is source-verified naming metadata, not retroactive support for the relation, lifecycle, or date. Prefer a unique canonical full title over a generic bare event head when comparing identity. Same thread, nearby dates, or matching generic heads are not sufficient by themselves. Return same_event, different_event, or uncertain for every pair and echo all identifiers exactly."""


class GmailTemporalEventIdentityConsumerError(ValueError):
    """Raised when a private identity consumer exceeds trusted authority."""


@dataclass(frozen=True)
class GmailTemporalEventIdentitySourceText:
    """Transient local source text bound to one immutable Gmail locator."""

    version: Literal["gmail_temporal_event_identity_source_text_v1"]
    gmail_account_key: str
    gmail_thread_id: str
    gmail_message_id: str
    source_sha256: str
    text: str = field(repr=False)


@dataclass(frozen=True)
class GmailTemporalEventIdentityMentionSurface:
    """One exact source slice recovered from a validated analysis mention."""

    version: Literal["gmail_temporal_event_identity_mention_surface_v1"]
    mention_id: str
    mention_type: str
    start: int
    end: int
    field: str
    surface: str = field(repr=False)


@dataclass(frozen=True)
class GmailTemporalEventIdentityUnitSurface:
    """Evidence and identity metadata for one plan unit, kept role-separated."""

    version: Literal["gmail_temporal_event_identity_unit_surface_v1"]
    unit_id: str
    message_order: int
    gmail_message_id: str
    source_sha256: str
    verifier_selected_evidence: tuple[GmailTemporalEventIdentityMentionSurface, ...] = (
        field(repr=False)
    )
    deterministic_identity_aliases: tuple[
        GmailTemporalEventIdentityMentionSurface, ...
    ] = field(repr=False)
    canonical_identity: GmailTemporalEventIdentityMentionSurface | None = field(
        repr=False
    )
    candidate_authorization: Literal[False] = False
    requires_defer: Literal[True] = True
    routable: Literal[False] = False


@dataclass(frozen=True)
class GmailTemporalEventIdentitySurfaceAuthority:
    """Transient, source-bound surface receipt for an exact identity plan."""

    version: Literal["gmail_temporal_event_identity_surface_authority_v1"]
    policy_version: Literal["gmail_temporal_event_identity_consumer_policy_v1"]
    policy_fingerprint: str
    surface_authority_fingerprint: str
    plan_fingerprint: str
    thread_authority_fingerprint: str
    units: tuple[GmailTemporalEventIdentityUnitSurface, ...] = field(repr=False)
    private_content: Literal[True] = True
    candidate_authorization: Literal[False] = False
    requires_defer: Literal[True] = True
    routable: Literal[False] = False


@dataclass(frozen=True)
class GmailTemporalEventIdentityPairRequest:
    """One bounded, sanitized page request; this module never executes it."""

    version: Literal["gmail_temporal_event_identity_pair_request_v1"]
    policy_version: Literal["gmail_temporal_event_identity_consumer_policy_v1"]
    policy_fingerprint: str
    request_fingerprint: str
    surface_authority_fingerprint: str
    plan_fingerprint: str
    page_fingerprint: str
    pair_ids: tuple[str, ...]
    unit_ids: tuple[str, ...]
    pair_count: int
    payload: str = field(repr=False)
    private_content: Literal[True] = True
    candidate_authorization: Literal[False] = False
    requires_defer: Literal[True] = True
    routable: Literal[False] = False


def gmail_temporal_event_identity_consumer_policy_fingerprint() -> str:
    """Bind the exact role separation, source checks, and retrieval export rule."""

    material = {
        "version": GMAIL_TEMPORAL_EVENT_IDENTITY_CONSUMER_POLICY_VERSION,
        "review_grouping_policy": (gmail_temporal_review_grouping_policy_fingerprint()),
        "sanitizer_version": GMAIL_SENSITIVE_DATA_VERSION,
        "source_authority": {
            "text_hash_must_match_current_locator": True,
            "mention_span_must_match_current_analysis": True,
            "plan_must_rebuild_exactly": True,
        },
        "role_separation": {
            "verifier_selected_mentions": "temporal_evidence",
            "complete_alias_family": "non_authorizing_identity_metadata",
            "canonical_title": "identity_metadata_only",
        },
        "request": {
            "version": GMAIL_TEMPORAL_EVENT_IDENTITY_PAIR_REQUEST_VERSION,
            "max_surface_chars": MAX_EVENT_IDENTITY_SURFACE_CHARS,
            "max_aliases_per_unit": MAX_EVENT_IDENTITY_ALIAS_SURFACES_PER_UNIT,
            "max_payload_bytes": MAX_EVENT_IDENTITY_PAIR_REQUEST_BYTES,
            "contract": GMAIL_TEMPORAL_EVENT_IDENTITY_PAIR_CONTRACT,
        },
        "retrieval": {
            "version": (GMAIL_TEMPORAL_EVENT_IDENTITY_RETRIEVAL_BINDING_POLICY_VERSION),
            "consumer_version": EXPERIMENT_VERSION,
            "alias_policy_version": (
                GMAIL_TEMPORAL_VERIFIED_EVENT_ALIAS_POLICY_VERSION
            ),
            "aliases": "canonical_qualified_event_title_only",
            "generic_bare_heads": "forbidden",
            "ambiguous_or_missing_canonical": "omit_binding",
        },
    }
    return "gteicp_" + hashlib.sha256(_canonical_bytes(material)).hexdigest()


def bind_gmail_temporal_event_identity_unit_surfaces(
    *,
    snapshot_authority: GmailTemporalThreadSnapshotAuthority,
    messages: tuple[GmailTemporalThreadMessageReview, ...],
    analysis_authorities: tuple[TemporalLeadAnalysis, ...],
    plan: GmailTemporalEventIdentityPlan,
    source_texts: tuple[GmailTemporalEventIdentitySourceText, ...],
) -> GmailTemporalEventIdentitySurfaceAuthority:
    """Recover exact surfaces only after rebuilding every upstream authority."""

    rebuilt = _rebuild_plan(
        snapshot_authority=snapshot_authority,
        messages=messages,
        analysis_authorities=analysis_authorities,
        plan=plan,
    )
    if rebuilt != plan:
        raise GmailTemporalEventIdentityConsumerError(
            "identity plan is not bound to the current thread inputs"
        )
    texts = _bind_source_texts(
        messages=messages,
        analysis_authorities=analysis_authorities,
        source_texts=source_texts,
    )

    units: list[GmailTemporalEventIdentityUnitSurface] = []
    for unit in plan.units:
        index = unit.message_order - 1
        if not 0 <= index < len(messages):
            raise GmailTemporalEventIdentityConsumerError(
                "identity unit message order is invalid"
            )
        message = messages[index]
        analysis = analysis_authorities[index]
        text = texts[index]
        mentions = {item.mention_id: item for item in analysis.mentions}
        if len(mentions) != len(analysis.mentions):
            raise GmailTemporalEventIdentityConsumerError(
                "identity mention authority is ambiguous"
            )
        selected = _surfaces_for_references(
            unit.subject_type_references,
            mention_ids=unit.subject_mention_ids,
            mentions=mentions,
            text=text,
        )
        aliases = _surfaces_for_references(
            unit.subject_alias_type_references,
            mention_ids=unit.subject_alias_mention_ids,
            mentions=mentions,
            text=text,
        )
        if len(aliases) > MAX_EVENT_IDENTITY_ALIAS_SURFACES_PER_UNIT:
            raise GmailTemporalEventIdentityConsumerError(
                "identity alias surface bound exceeded"
            )
        aliases_by_id = {item.mention_id: item for item in aliases}
        canonical = (
            aliases_by_id.get(unit.canonical_subject_mention_id)
            if unit.canonical_subject_mention_id is not None
            else None
        )
        if unit.canonical_subject_mention_id is not None and (
            canonical is None or canonical.mention_type != "event_title_candidate"
        ):
            raise GmailTemporalEventIdentityConsumerError(
                "canonical identity exceeds alias authority"
            )
        units.append(
            GmailTemporalEventIdentityUnitSurface(
                version=_UNIT_SURFACE_VERSION,
                unit_id=unit.unit_id,
                message_order=unit.message_order,
                gmail_message_id=message.source.gmail_message_id,
                source_sha256=message.source.source_sha256,
                verifier_selected_evidence=selected,
                deterministic_identity_aliases=aliases,
                canonical_identity=canonical,
            )
        )

    policy_fingerprint = gmail_temporal_event_identity_consumer_policy_fingerprint()
    material = {
        "version": _SURFACE_AUTHORITY_VERSION,
        "policy_version": GMAIL_TEMPORAL_EVENT_IDENTITY_CONSUMER_POLICY_VERSION,
        "policy_fingerprint": policy_fingerprint,
        "plan_fingerprint": plan.plan_fingerprint,
        "thread_authority_fingerprint": plan.thread_authority_fingerprint,
        "units": [asdict(item) for item in units],
        "private_content": True,
        "candidate_authorization": False,
        "requires_defer": True,
        "routable": False,
    }
    result = GmailTemporalEventIdentitySurfaceAuthority(
        version=_SURFACE_AUTHORITY_VERSION,
        policy_version=GMAIL_TEMPORAL_EVENT_IDENTITY_CONSUMER_POLICY_VERSION,
        policy_fingerprint=policy_fingerprint,
        surface_authority_fingerprint=(
            "gteisa_" + hashlib.sha256(_canonical_bytes(material)).hexdigest()
        ),
        plan_fingerprint=plan.plan_fingerprint,
        thread_authority_fingerprint=plan.thread_authority_fingerprint,
        units=tuple(units),
    )
    _validate_surface_authority(result, plan=plan)
    return result


def build_gmail_temporal_event_identity_pair_requests(
    *,
    snapshot_authority: GmailTemporalThreadSnapshotAuthority,
    messages: tuple[GmailTemporalThreadMessageReview, ...],
    analysis_authorities: tuple[TemporalLeadAnalysis, ...],
    plan: GmailTemporalEventIdentityPlan,
    source_texts: tuple[GmailTemporalEventIdentitySourceText, ...],
) -> tuple[GmailTemporalEventIdentityPairRequest, ...]:
    """Build sanitized page payloads without calling or trusting a provider."""

    authority = bind_gmail_temporal_event_identity_unit_surfaces(
        snapshot_authority=snapshot_authority,
        messages=messages,
        analysis_authorities=analysis_authorities,
        plan=plan,
        source_texts=source_texts,
    )
    units = {item.unit_id: item for item in authority.units}
    pairs = {item.pair_id: item for item in plan.pairs}
    requests: list[GmailTemporalEventIdentityPairRequest] = []
    for page in plan.pages:
        page_pairs = tuple(pairs[pair_id] for pair_id in page.pair_ids)
        unit_ids = tuple(
            sorted(
                {
                    unit_id
                    for pair in page_pairs
                    for unit_id in (pair.left_unit_id, pair.right_unit_id)
                }
            )
        )
        request_units = tuple(units[unit_id] for unit_id in unit_ids)
        material = {
            "version": GMAIL_TEMPORAL_EVENT_IDENTITY_PAIR_REQUEST_VERSION,
            "policy_version": GMAIL_TEMPORAL_EVENT_IDENTITY_CONSUMER_POLICY_VERSION,
            "policy_fingerprint": authority.policy_fingerprint,
            "surface_authority_fingerprint": (authority.surface_authority_fingerprint),
            "plan_fingerprint": plan.plan_fingerprint,
            "page_fingerprint": page.page_fingerprint,
            "contract": GMAIL_TEMPORAL_EVENT_IDENTITY_PAIR_CONTRACT,
            "response_schema": {
                "request_fingerprint": "echo_exactly",
                "verdicts": [
                    {
                        "pair_id": "echo_exactly",
                        "verdict": [
                            "same_event",
                            "different_event",
                            "uncertain",
                        ],
                    }
                ],
            },
            "units": [_request_unit_payload(item, plan=plan) for item in request_units],
            "pairs": [asdict(item) for item in page_pairs],
            "candidate_authorization": False,
            "requires_defer": True,
            "routable": False,
        }
        sanitized = sanitize_gmail_model_payload(material)
        request_fingerprint = (
            "gteirq_" + hashlib.sha256(_canonical_bytes(sanitized)).hexdigest()
        )
        payload = _canonical_bytes(
            {**sanitized, "request_fingerprint": request_fingerprint}
        ).decode("utf-8")
        if len(payload.encode("utf-8")) > MAX_EVENT_IDENTITY_PAIR_REQUEST_BYTES:
            raise GmailTemporalEventIdentityConsumerError(
                "identity pair request payload bound exceeded"
            )
        requests.append(
            GmailTemporalEventIdentityPairRequest(
                version=GMAIL_TEMPORAL_EVENT_IDENTITY_PAIR_REQUEST_VERSION,
                policy_version=(GMAIL_TEMPORAL_EVENT_IDENTITY_CONSUMER_POLICY_VERSION),
                policy_fingerprint=authority.policy_fingerprint,
                request_fingerprint=request_fingerprint,
                surface_authority_fingerprint=(authority.surface_authority_fingerprint),
                plan_fingerprint=plan.plan_fingerprint,
                page_fingerprint=page.page_fingerprint,
                pair_ids=page.pair_ids,
                unit_ids=unit_ids,
                pair_count=len(page_pairs),
                payload=payload,
            )
        )
    return tuple(requests)


def build_gmail_temporal_verified_event_bindings(
    *,
    snapshot_authority: GmailTemporalThreadSnapshotAuthority,
    messages: tuple[GmailTemporalThreadMessageReview, ...],
    analysis_authorities: tuple[TemporalLeadAnalysis, ...],
    plan: GmailTemporalEventIdentityPlan,
    resolution: GmailTemporalEventIdentityResolution,
    source_texts: tuple[GmailTemporalEventIdentitySourceText, ...],
    prior_resolution: GmailTemporalEventIdentityResolution | None = None,
) -> tuple[GmailTemporalVerifiedEventBinding, ...]:
    """Export only qualified canonical names from validated resolution clusters."""

    authority = bind_gmail_temporal_event_identity_unit_surfaces(
        snapshot_authority=snapshot_authority,
        messages=messages,
        analysis_authorities=analysis_authorities,
        plan=plan,
        source_texts=source_texts,
    )
    try:
        validate_gmail_temporal_event_identity_resolution(
            plan=plan,
            resolution=resolution,
            prior_resolution=prior_resolution,
        )
    except ValueError as exc:
        raise GmailTemporalEventIdentityConsumerError(
            "identity resolution is invalid or stale"
        ) from exc

    surfaces = {item.unit_id: item for item in authority.units}
    bindings: list[GmailTemporalVerifiedEventBinding] = []
    for cluster in resolution.clusters:
        aliases_by_key: dict[str, str] = {}
        for unit_id in cluster.unit_ids:
            canonical = surfaces[unit_id].canonical_identity
            if canonical is None or not _qualified_event_alias(canonical.surface):
                continue
            key = gmail_temporal_verified_event_alias_key(canonical.surface)
            if key is None:
                continue
            aliases_by_key.setdefault(key, canonical.surface)
        aliases = tuple(aliases_by_key[key] for key in sorted(aliases_by_key))
        if not aliases:
            continue
        bindings.append(
            GmailTemporalVerifiedEventBinding(
                event_identity_key=cluster.event_identity_key,
                aliases=aliases,
            )
        )
    return tuple(sorted(bindings, key=lambda item: item.event_identity_key))


def _rebuild_plan(
    *,
    snapshot_authority: GmailTemporalThreadSnapshotAuthority,
    messages: tuple[GmailTemporalThreadMessageReview, ...],
    analysis_authorities: tuple[TemporalLeadAnalysis, ...],
    plan: GmailTemporalEventIdentityPlan,
) -> GmailTemporalEventIdentityPlan:
    if not isinstance(plan, GmailTemporalEventIdentityPlan):
        raise GmailTemporalEventIdentityConsumerError("identity plan is invalid")
    try:
        return plan_gmail_temporal_event_identity(
            snapshot_authority=snapshot_authority,
            messages=messages,
            analysis_authorities=analysis_authorities,
            pairs_per_page=plan.pairs_per_page,
        )
    except (TypeError, ValueError) as exc:
        raise GmailTemporalEventIdentityConsumerError(
            "identity source authority is invalid or stale"
        ) from exc


def _bind_source_texts(
    *,
    messages: tuple[GmailTemporalThreadMessageReview, ...],
    analysis_authorities: tuple[TemporalLeadAnalysis, ...],
    source_texts: tuple[GmailTemporalEventIdentitySourceText, ...],
) -> tuple[str, ...]:
    if (
        not isinstance(source_texts, tuple)
        or len(source_texts) != len(messages)
        or any(
            not isinstance(item, GmailTemporalEventIdentitySourceText)
            for item in source_texts
        )
    ):
        raise GmailTemporalEventIdentityConsumerError(
            "identity source text coverage is incomplete"
        )
    output: list[str] = []
    for message, analysis, source in zip(
        messages,
        analysis_authorities,
        source_texts,
        strict=True,
    ):
        locator = message.source
        if (
            source.version != _SOURCE_TEXT_VERSION
            or source.gmail_account_key != locator.gmail_account_key
            or source.gmail_thread_id != locator.gmail_thread_id
            or source.gmail_message_id != locator.gmail_message_id
            or source.source_sha256 != locator.source_sha256
            or analysis.source_sha256 != locator.source_sha256
            or not isinstance(source.text, str)
            or hashlib.sha256(source.text.encode("utf-8")).hexdigest()
            != locator.source_sha256
        ):
            raise GmailTemporalEventIdentityConsumerError(
                "identity source text is invalid or stale"
            )
        output.append(source.text)
    return tuple(output)


def _surfaces_for_references(
    references: tuple[tuple[str, str], ...],
    *,
    mention_ids: tuple[str, ...],
    mentions: dict[str, TemporalMention],
    text: str,
) -> tuple[GmailTemporalEventIdentityMentionSurface, ...]:
    if tuple(mention_id for mention_id, _ in references) != mention_ids:
        raise GmailTemporalEventIdentityConsumerError(
            "identity mention references are not ordered exactly"
        )
    output: list[GmailTemporalEventIdentityMentionSurface] = []
    for mention_id, mention_type in references:
        mention = mentions.get(mention_id)
        if (
            mention is None
            or mention.mention_type != mention_type
            or isinstance(mention.start, bool)
            or isinstance(mention.end, bool)
            or not isinstance(mention.start, int)
            or not isinstance(mention.end, int)
            or not 0 <= mention.start < mention.end <= len(text)
        ):
            raise GmailTemporalEventIdentityConsumerError(
                "identity mention span or type is invalid"
            )
        surface = text[mention.start : mention.end]
        if (
            not surface.strip()
            or "\x00" in surface
            or len(surface) > MAX_EVENT_IDENTITY_SURFACE_CHARS
        ):
            raise GmailTemporalEventIdentityConsumerError(
                "identity mention surface is invalid or too large"
            )
        output.append(
            GmailTemporalEventIdentityMentionSurface(
                version=_MENTION_SURFACE_VERSION,
                mention_id=mention.mention_id,
                mention_type=mention.mention_type,
                start=mention.start,
                end=mention.end,
                field=mention.field,
                surface=surface,
            )
        )
    return tuple(output)


def _request_unit_payload(
    surface: GmailTemporalEventIdentityUnitSurface,
    *,
    plan: GmailTemporalEventIdentityPlan,
) -> dict[str, object]:
    unit = next(item for item in plan.units if item.unit_id == surface.unit_id)
    return {
        "unit_id": unit.unit_id,
        "message_order": unit.message_order,
        "temporal_claim": {
            "relation": unit.relation,
            "kind": unit.kind,
            "lifecycle": unit.lifecycle,
            "normalized_value": unit.normalized_value,
        },
        "verifier_selected_evidence": [
            _mention_payload(item) for item in surface.verifier_selected_evidence
        ],
        "deterministic_identity_metadata": {
            "authority": "source_verified_non_authorizing_alias_family",
            "alias_mentions": [
                _mention_payload(item)
                for item in surface.deterministic_identity_aliases
            ],
            "canonical_full_title": (
                _mention_payload(surface.canonical_identity)
                if surface.canonical_identity is not None
                else None
            ),
        },
    }


def _mention_payload(
    value: GmailTemporalEventIdentityMentionSurface,
) -> dict[str, object]:
    return {
        "mention_id": value.mention_id,
        "mention_type": value.mention_type,
        "start": value.start,
        "end": value.end,
        "field": value.field,
        "surface": value.surface,
    }


def _validate_surface_authority(
    authority: GmailTemporalEventIdentitySurfaceAuthority,
    *,
    plan: GmailTemporalEventIdentityPlan,
) -> None:
    material = {
        "version": authority.version,
        "policy_version": authority.policy_version,
        "policy_fingerprint": authority.policy_fingerprint,
        "plan_fingerprint": authority.plan_fingerprint,
        "thread_authority_fingerprint": authority.thread_authority_fingerprint,
        "units": [asdict(item) for item in authority.units],
        "private_content": authority.private_content,
        "candidate_authorization": authority.candidate_authorization,
        "requires_defer": authority.requires_defer,
        "routable": authority.routable,
    }
    expected_fingerprint = (
        "gteisa_" + hashlib.sha256(_canonical_bytes(material)).hexdigest()
    )
    if (
        authority.version != _SURFACE_AUTHORITY_VERSION
        or authority.policy_version
        != GMAIL_TEMPORAL_EVENT_IDENTITY_CONSUMER_POLICY_VERSION
        or authority.policy_fingerprint
        != gmail_temporal_event_identity_consumer_policy_fingerprint()
        or authority.surface_authority_fingerprint != expected_fingerprint
        or authority.plan_fingerprint != plan.plan_fingerprint
        or authority.thread_authority_fingerprint != plan.thread_authority_fingerprint
        or tuple(item.unit_id for item in authority.units)
        != tuple(item.unit_id for item in plan.units)
        or authority.private_content is not True
        or authority.candidate_authorization is not False
        or authority.requires_defer is not True
        or authority.routable is not False
    ):
        raise GmailTemporalEventIdentityConsumerError(
            "identity surface authority is invalid or stale"
        )


def _qualified_event_alias(value: object) -> bool:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > 256
        or any(character in value for character in "\x00\r\n")
    ):
        return False
    tokens = tuple(
        unicodedata.normalize("NFKC", match.group(0)).casefold()
        for match in _TOKEN.finditer(value)
    )
    return bool(tokens) and any(
        token not in _GENERIC_EVENT_NAME_TOKENS and not token.isdigit()
        for token in tokens
    )


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
