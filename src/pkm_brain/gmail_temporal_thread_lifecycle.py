from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Literal, Mapping

from .gmail_temporal_persistence import GmailTemporalSourceLocator
from .gmail_temporal_leads import TemporalLeadAnalysis
from .gmail_temporal_review import (
    GmailTemporalReviewArtifact,
    GmailTemporalReviewError,
    GmailTemporalReviewGroup,
    GmailTemporalReviewHypothesis,
    GmailTemporalReviewProjection,
    gmail_temporal_review_projection_payload,
)

if TYPE_CHECKING:
    from .gmail_temporal_event_identity import (
        GmailTemporalEventIdentityPlan,
        GmailTemporalEventIdentityResolution,
    )


_PROJECTION_VERSION = "gmail_temporal_thread_lifecycle_projection_v3"
_AUTHORITY_VERSION = "gmail_temporal_thread_snapshot_authority_v2"
_MESSAGE_AUTHORITY_VERSION = "gmail_temporal_thread_message_authority_v2"
_MESSAGE_REVIEW_VERSION = "gmail_temporal_thread_message_review_v1"
_IDENTITY_ASSERTION_VERSION = "gmail_temporal_event_identity_assertion_v2"
_SOURCE_REF_VERSION = "gmail_temporal_lifecycle_source_ref_v1"
_OCCURRENCE_VERSION = "gmail_temporal_lifecycle_occurrence_v1"
_UNRESOLVED_VERSION = "gmail_temporal_lifecycle_unresolved_alternative_v1"
_EVENT_IDENTITY_UNIT_VERSION = "gmail_temporal_event_identity_unit_v3"
_EVENT_IDENTITY_KEY_VERSION = "gmail_temporal_stable_event_key_v1"

_OPAQUE_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,511}$")
_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")
_VERIFIED_IDENTITY_AUTHORITIES = frozenset(
    {"source_bound_self_identity", "external_verified", "owner_verified"}
)

IdentityVerification = Literal[
    "unverified",
    "source_bound_self_identity",
    "external_verified",
    "owner_verified",
]
ResolvedLifecycleStatus = Literal["scheduled", "cancelled", "completed"]
LifecycleStatus = Literal["scheduled", "cancelled", "completed", "unresolved"]
OccurrenceState = Literal["scheduled", "superseded", "cancelled", "completed"]
LifecycleSourceKind = Literal["artifact", "cluster_review", "group"]


class GmailTemporalThreadLifecycleError(ValueError):
    """Raised when a thread view cannot be derived from current immutable input."""


@dataclass(frozen=True)
class GmailTemporalThreadMessageAuthority:
    """One current ledger head bound to its current immutable source locator."""

    version: Literal["gmail_temporal_thread_message_authority_v2"]
    source: GmailTemporalSourceLocator
    pipeline_scope: str
    current_review_run_id: str
    current_head_generation: int
    current_analysis_fingerprint: str
    current_projection_fingerprint: str
    current_projection_sha256: str


@dataclass(frozen=True)
class GmailTemporalThreadSnapshotAuthority:
    """Trusted current ordering and heads for one immutable Gmail thread revision."""

    version: Literal["gmail_temporal_thread_snapshot_authority_v2"]
    messages: tuple[GmailTemporalThreadMessageAuthority, ...]
    prior_event_identity_resolution_fingerprint: str | None = None


@dataclass(frozen=True)
class GmailTemporalEventIdentityAssertion:
    """Explicit provenance for an event-identity assertion.

    Source-bound self authority binds one source event unit only to itself.
    Cross-message reconciliation still requires an owner- or externally-verified
    shared key. Even then, the assertion only authorizes this review-only
    projection; it can never authorize a fact, reminder, action, or route.
    """

    version: Literal["gmail_temporal_event_identity_assertion_v2"]
    unit_id: str
    projection_fingerprint: str
    artifact_id: str
    hypothesis_id: str
    event_identity_key: str
    verification: IdentityVerification
    provenance_ref: str
    candidate_authorization: Literal[False] = False
    requires_defer: Literal[True] = True
    routable: Literal[False] = False


@dataclass(frozen=True)
class GmailTemporalThreadMessageReview:
    """One append-only review run and optional identity assertions."""

    version: Literal["gmail_temporal_thread_message_review_v1"]
    source: GmailTemporalSourceLocator
    review_run_id: str
    projection: GmailTemporalReviewProjection
    identity_assertions: tuple[GmailTemporalEventIdentityAssertion, ...] = ()
    candidate_authorization: Literal[False] = False
    requires_defer: Literal[True] = True
    routable: Literal[False] = False


@dataclass(frozen=True)
class GmailTemporalLifecycleSourceRef:
    """Exact immutable source unit used by derived lifecycle review metadata."""

    version: Literal["gmail_temporal_lifecycle_source_ref_v1"]
    ref_id: str
    gmail_message_id: str
    review_run_id: str
    projection_fingerprint: str
    source_kind: LifecycleSourceKind
    source_id: str
    hypothesis_id: str | None
    routable: Literal[False] = False


@dataclass(frozen=True)
class GmailTemporalLifecycleOccurrence:
    """One retained occurrence revision in an event's review-only lifecycle."""

    version: Literal["gmail_temporal_lifecycle_occurrence_v1"]
    occurrence_id: str
    normalized_value: str
    state: OccurrenceState
    superseded_by_occurrence_id: str | None
    source_refs: tuple[GmailTemporalLifecycleSourceRef, ...]
    derived_review_metadata: Literal[True] = True
    candidate_authorization: Literal[False] = False
    requires_defer: Literal[True] = True
    routable: Literal[False] = False


@dataclass(frozen=True)
class GmailTemporalEventLifecycleView:
    """Event-centered lifecycle metadata; never an event entity or fact."""

    version: Literal["gmail_temporal_event_lifecycle_view_v1"]
    event_identity_key: str
    entity_type: Literal["event"]
    current_status: LifecycleStatus
    last_unambiguous_status: ResolvedLifecycleStatus | None
    current_occurrence_id: str | None
    occurrences: tuple[GmailTemporalLifecycleOccurrence, ...]
    identity_provenance: tuple[GmailTemporalEventIdentityAssertion, ...]
    derived_review_metadata: Literal[True] = True
    candidate_authorization: Literal[False] = False
    requires_defer: Literal[True] = True
    routable: Literal[False] = False


@dataclass(frozen=True)
class GmailTemporalLifecycleUnresolvedAlternative:
    """Ambiguous identity or state retained without changing resolved state."""

    version: Literal["gmail_temporal_lifecycle_unresolved_alternative_v1"]
    alternative_id: str
    reason: str
    possible_event_identity_keys: tuple[str, ...]
    possible_statuses: tuple[str, ...]
    source_refs: tuple[GmailTemporalLifecycleSourceRef, ...]
    derived_review_metadata: Literal[True] = True
    candidate_authorization: Literal[False] = False
    requires_defer: Literal[True] = True
    routable: Literal[False] = False


@dataclass(frozen=True)
class GmailTemporalThreadLifecycleProjection:
    """Pure, deterministic lifecycle view over one current Gmail thread."""

    version: Literal["gmail_temporal_thread_lifecycle_projection_v3"]
    projection_fingerprint: str
    snapshot_authority: GmailTemporalThreadSnapshotAuthority
    source_messages: tuple[GmailTemporalThreadMessageReview, ...]
    event_identity_analysis_authorities: tuple[TemporalLeadAnalysis, ...] | None
    event_identity_plan: GmailTemporalEventIdentityPlan | None
    event_identity_resolution: GmailTemporalEventIdentityResolution | None
    event_identity_prior_resolution: GmailTemporalEventIdentityResolution | None
    events: tuple[GmailTemporalEventLifecycleView, ...]
    unresolved_alternatives: tuple[GmailTemporalLifecycleUnresolvedAlternative, ...]
    complete: Literal[True]
    derived_review_metadata: Literal[True] = True
    candidate_authorization: Literal[False] = False
    requires_defer: Literal[True] = True
    routable: Literal[False] = False


@dataclass(frozen=True)
class _IdentityResolution:
    key: str | None
    asserted_keys: tuple[str, ...]
    verified_assertions: tuple[GmailTemporalEventIdentityAssertion, ...]
    reason: str | None


@dataclass
class _MutableOccurrence:
    occurrence_id: str
    normalized_value: str
    state: OccurrenceState
    superseded_by_occurrence_id: str | None
    source_refs: list[GmailTemporalLifecycleSourceRef]


@dataclass
class _MutableEvent:
    event_identity_key: str
    current_status: ResolvedLifecycleStatus | None
    current_occurrence_id: str | None
    occurrences: list[_MutableOccurrence]
    identity_provenance: list[GmailTemporalEventIdentityAssertion]
    ambiguous: bool = False


def project_gmail_temporal_thread_lifecycle(
    *,
    snapshot_authority: GmailTemporalThreadSnapshotAuthority,
    messages: tuple[GmailTemporalThreadMessageReview, ...],
    event_identity_analysis_authorities: tuple[TemporalLeadAnalysis, ...] | None = None,
    event_identity_plan: GmailTemporalEventIdentityPlan | None = None,
    event_identity_resolution: GmailTemporalEventIdentityResolution | None = None,
    event_identity_prior_resolution: GmailTemporalEventIdentityResolution | None = None,
) -> GmailTemporalThreadLifecycleProjection:
    """Derive non-routable event lifecycle views from ordered message reviews.

    Current source/head authority is an explicit input so stale projections fail
    before reduction. Every resolver-verified assertion is rebound to its exact
    analyses, plan, resolution, and declared parent. Cross-message reconciliation
    requires one externally verified shared key; owner authority remains closed
    until a trusted owner-receipt ledger exists. Thread membership or matching
    dates are never identity evidence.
    """

    normalized_authority, normalized_messages = _validate_inputs(
        snapshot_authority=snapshot_authority,
        messages=messages,
    )
    _validate_event_identity_authority(
        snapshot_authority=normalized_authority,
        messages=normalized_messages,
        event_identity_analysis_authorities=event_identity_analysis_authorities,
        event_identity_plan=event_identity_plan,
        event_identity_resolution=event_identity_resolution,
        event_identity_prior_resolution=event_identity_prior_resolution,
    )
    events: dict[str, _MutableEvent] = {}
    unresolved: list[GmailTemporalLifecycleUnresolvedAlternative] = []

    for message in normalized_messages:
        artifacts = {item.artifact_id: item for item in message.projection.artifacts}
        assertions = _assertions_by_hypothesis(message)
        for group in message.projection.groups:
            if group.coverage != "complete":
                refs = _group_source_refs(message, group, artifacts)
                keys = _asserted_keys_for_refs(message, refs, assertions)
                _add_unresolved(
                    unresolved,
                    events,
                    reason="incomplete_or_conflicted_message_group",
                    keys=keys,
                    statuses=_statuses_for_group(group, artifacts),
                    refs=refs,
                )
                continue
            if group.kind == "reschedule":
                _apply_reschedule(
                    message=message,
                    group=group,
                    artifacts=artifacts,
                    assertions=assertions,
                    events=events,
                    unresolved=unresolved,
                )
                continue
            if group.kind in {"alternatives", "split_semantics"}:
                refs = _group_source_refs(message, group, artifacts)
                keys = _asserted_keys_for_refs(message, refs, assertions)
                _add_unresolved(
                    unresolved,
                    events,
                    reason=(
                        "source_alternatives_unresolved"
                        if group.kind == "alternatives"
                        else "split_semantics_unresolved"
                    ),
                    keys=keys,
                    statuses=_statuses_for_group(group, artifacts),
                    refs=refs,
                )
                continue
            _apply_single_group(
                message=message,
                group=group,
                artifacts=artifacts,
                assertions=assertions,
                events=events,
                unresolved=unresolved,
            )

    event_views = tuple(_freeze_event(events[key]) for key in sorted(events))
    unresolved_tuple = tuple(unresolved)
    material = {
        "version": _PROJECTION_VERSION,
        "snapshot_authority": asdict(normalized_authority),
        "source_messages": [asdict(item) for item in normalized_messages],
        "event_identity_analysis_authorities": (
            [asdict(item) for item in event_identity_analysis_authorities]
            if event_identity_analysis_authorities is not None
            else None
        ),
        "event_identity_plan": (
            asdict(event_identity_plan) if event_identity_plan is not None else None
        ),
        "event_identity_resolution": (
            asdict(event_identity_resolution)
            if event_identity_resolution is not None
            else None
        ),
        "event_identity_prior_resolution": (
            asdict(event_identity_prior_resolution)
            if event_identity_prior_resolution is not None
            else None
        ),
        "events": [asdict(item) for item in event_views],
        "unresolved_alternatives": [asdict(item) for item in unresolved_tuple],
        "complete": True,
        "derived_review_metadata": True,
        "candidate_authorization": False,
        "requires_defer": True,
        "routable": False,
    }
    fingerprint = "gtlp_" + hashlib.sha256(_canonical_bytes(material)).hexdigest()
    return GmailTemporalThreadLifecycleProjection(
        version="gmail_temporal_thread_lifecycle_projection_v3",
        projection_fingerprint=fingerprint,
        snapshot_authority=normalized_authority,
        source_messages=normalized_messages,
        event_identity_analysis_authorities=event_identity_analysis_authorities,
        event_identity_plan=event_identity_plan,
        event_identity_resolution=event_identity_resolution,
        event_identity_prior_resolution=event_identity_prior_resolution,
        events=event_views,
        unresolved_alternatives=unresolved_tuple,
        complete=True,
    )


def gmail_temporal_thread_lifecycle_projection_payload(
    projection: GmailTemporalThreadLifecycleProjection,
) -> dict[str, Any]:
    """Return validated canonical JSON-safe lifecycle review metadata."""

    if not isinstance(projection, GmailTemporalThreadLifecycleProjection):
        raise GmailTemporalThreadLifecycleError(
            "thread lifecycle projection is invalid"
        )
    material = {
        key: value
        for key, value in asdict(projection).items()
        if key != "projection_fingerprint"
    }
    expected = "gtlp_" + hashlib.sha256(_canonical_bytes(material)).hexdigest()
    if (
        projection.version != _PROJECTION_VERSION
        or projection.projection_fingerprint != expected
        or projection.complete is not True
        or projection.derived_review_metadata is not True
        or projection.candidate_authorization is not False
        or projection.requires_defer is not True
        or projection.routable is not False
        or any(item.routable for item in projection.events)
        or any(item.routable for item in projection.unresolved_alternatives)
    ):
        raise GmailTemporalThreadLifecycleError("thread lifecycle projection is stale")
    expected_projection = project_gmail_temporal_thread_lifecycle(
        snapshot_authority=projection.snapshot_authority,
        messages=projection.source_messages,
        event_identity_analysis_authorities=(
            projection.event_identity_analysis_authorities
        ),
        event_identity_plan=projection.event_identity_plan,
        event_identity_resolution=projection.event_identity_resolution,
        event_identity_prior_resolution=projection.event_identity_prior_resolution,
    )
    if projection != expected_projection:
        raise GmailTemporalThreadLifecycleError(
            "thread lifecycle projection does not match its deterministic derivation"
        )
    return asdict(projection)


def canonical_gmail_temporal_thread_lifecycle_projection_bytes(
    projection: GmailTemporalThreadLifecycleProjection,
) -> bytes:
    return _canonical_bytes(
        gmail_temporal_thread_lifecycle_projection_payload(projection)
    )


def validate_gmail_temporal_thread_review_inputs(
    *,
    snapshot_authority: GmailTemporalThreadSnapshotAuthority,
    messages: tuple[GmailTemporalThreadMessageReview, ...],
) -> tuple[
    GmailTemporalThreadSnapshotAuthority,
    tuple[GmailTemporalThreadMessageReview, ...],
]:
    """Validate and canonicalize current ordered thread review authority."""

    return _validate_inputs(
        snapshot_authority=snapshot_authority,
        messages=messages,
    )


def gmail_temporal_event_identity_unit_id(
    *,
    source_anchor: Mapping[str, str],
    artifact: GmailTemporalReviewArtifact,
    hypothesis: GmailTemporalReviewHypothesis,
) -> str:
    """Derive the stable source-bound identity-unit key shared by both reducers."""

    expected_source_keys = {
        "gmail_account_key",
        "gmail_thread_id",
        "gmail_message_id",
        "source_sha256",
    }
    if (
        not isinstance(source_anchor, Mapping)
        or set(source_anchor) != expected_source_keys
        or any(
            not isinstance(source_anchor[key], str) or not source_anchor[key]
            for key in expected_source_keys
        )
        or not isinstance(artifact, GmailTemporalReviewArtifact)
        or not isinstance(hypothesis, GmailTemporalReviewHypothesis)
        or hypothesis not in artifact.hypotheses
    ):
        raise GmailTemporalThreadLifecycleError(
            "event identity unit source authority is invalid"
        )
    material = {
        "version": _EVENT_IDENTITY_UNIT_VERSION,
        "source_anchor": dict(source_anchor),
        "artifact_semantics": {
            "version": artifact.version,
            "artifact_id": artifact.artifact_id,
            "kind": artifact.kind,
            "evidence_status": artifact.evidence_status,
        },
        "hypothesis_semantics": {
            "version": hypothesis.version,
            "hypothesis_id": hypothesis.hypothesis_id,
            "expression_id": hypothesis.expression_id,
            "subject_mention_ids": hypothesis.subject_mention_ids,
            "subject_type_references": hypothesis.subject_type_references,
            "subject_alias_mention_ids": hypothesis.subject_alias_mention_ids,
            "subject_alias_type_references": (hypothesis.subject_alias_type_references),
            "canonical_subject_mention_id": (hypothesis.canonical_subject_mention_id),
            "lifecycle_mention_ids": hypothesis.lifecycle_mention_ids,
            "relation": hypothesis.relation,
            "kind": hypothesis.kind,
            "lifecycle": hypothesis.lifecycle,
            "normalized_value": hypothesis.normalized_value,
            "candidate_requires_defer": hypothesis.candidate_requires_defer,
        },
    }
    return "gteiu_" + hashlib.sha256(_canonical_bytes(material)).hexdigest()


def gmail_temporal_source_bound_event_identity_key(unit_id: str) -> str:
    """Derive the event key for a unit that is asserted only equal to itself."""

    normalized_unit_id = _opaque(unit_id, "event identity unit id")
    digest = hashlib.sha256(
        _canonical_bytes(
            {
                "version": _EVENT_IDENTITY_KEY_VERSION,
                "anchor_unit_id": normalized_unit_id,
            }
        )
    ).hexdigest()
    return f"gmail-event:{digest}"


def gmail_temporal_source_bound_self_provenance(unit_id: str) -> str:
    """Return provenance that can be re-bound to the exact source unit locally."""

    return f"gteiself:{_opaque(unit_id, 'event identity unit id')}"


def _validate_event_identity_authority(
    *,
    snapshot_authority: GmailTemporalThreadSnapshotAuthority,
    messages: tuple[GmailTemporalThreadMessageReview, ...],
    event_identity_analysis_authorities: tuple[TemporalLeadAnalysis, ...] | None,
    event_identity_plan: GmailTemporalEventIdentityPlan | None,
    event_identity_resolution: GmailTemporalEventIdentityResolution | None,
    event_identity_prior_resolution: GmailTemporalEventIdentityResolution | None,
) -> None:
    assertions = tuple(
        assertion for message in messages for assertion in message.identity_assertions
    )
    if any(item.verification == "owner_verified" for item in assertions):
        raise GmailTemporalThreadLifecycleError(
            "owner-verified identity requires trusted owner-lineage authority"
        )
    has_resolver_verified = any(
        item.verification in {"source_bound_self_identity", "external_verified"}
        for item in assertions
    )
    if (
        len(
            {
                event_identity_analysis_authorities is None,
                event_identity_plan is None,
                event_identity_resolution is None,
            }
        )
        != 1
    ):
        raise GmailTemporalThreadLifecycleError(
            "event identity analyses, plan, and resolution must be supplied together"
        )
    if event_identity_plan is None:
        if event_identity_prior_resolution is not None:
            raise GmailTemporalThreadLifecycleError(
                "prior identity resolution requires current plan authority"
            )
        if has_resolver_verified:
            raise GmailTemporalThreadLifecycleError(
                "resolver-verified identity requires complete plan and resolution authority"
            )
        return

    # Lifecycle consumes the resolver's exact binding, never a caller-authored
    # assertion tuple. Import locally to keep the lifecycle/identity type split
    # acyclic while reusing the resolver's full structural validation boundary.
    from .gmail_temporal_event_identity import (  # noqa: PLC0415
        GmailTemporalEventIdentityError,
        bind_gmail_temporal_event_identity_resolution,
    )

    bare_messages = tuple(
        replace(message, identity_assertions=()) for message in messages
    )
    try:
        expected_messages = bind_gmail_temporal_event_identity_resolution(
            snapshot_authority=snapshot_authority,
            messages=bare_messages,
            analysis_authorities=event_identity_analysis_authorities,
            plan=event_identity_plan,
            resolution=event_identity_resolution,
            prior_resolution=event_identity_prior_resolution,
        )
    except GmailTemporalEventIdentityError as exc:
        raise GmailTemporalThreadLifecycleError(
            "event identity plan or resolution authority is invalid"
        ) from exc
    if expected_messages != messages:
        raise GmailTemporalThreadLifecycleError(
            "message identity assertions do not match the exact resolver binding"
        )


def _validate_inputs(
    *,
    snapshot_authority: GmailTemporalThreadSnapshotAuthority,
    messages: tuple[GmailTemporalThreadMessageReview, ...],
) -> tuple[
    GmailTemporalThreadSnapshotAuthority,
    tuple[GmailTemporalThreadMessageReview, ...],
]:
    if (
        not isinstance(snapshot_authority, GmailTemporalThreadSnapshotAuthority)
        or snapshot_authority.version != _AUTHORITY_VERSION
        or not isinstance(snapshot_authority.messages, tuple)
        or not snapshot_authority.messages
        or not isinstance(messages, tuple)
        or len(messages) != len(snapshot_authority.messages)
    ):
        raise GmailTemporalThreadLifecycleError(
            "complete ordered snapshot authority is required"
        )
    prior_resolution_fingerprint = (
        snapshot_authority.prior_event_identity_resolution_fingerprint
    )
    if (
        prior_resolution_fingerprint is not None
        and _OPAQUE_KEY.fullmatch(prior_resolution_fingerprint) is None
    ):
        raise GmailTemporalThreadLifecycleError(
            "prior event identity resolution authority is invalid"
        )

    normalized_authorities: list[GmailTemporalThreadMessageAuthority] = []
    normalized_messages: list[GmailTemporalThreadMessageReview] = []
    base_scope: tuple[str, str, str, str, str] | None = None
    previous_order: tuple[datetime, str] | None = None
    previous_end: int | None = None
    seen_messages: set[str] = set()
    seen_runs: set[str] = set()
    pipeline_scope: str | None = None

    for authority, message in zip(snapshot_authority.messages, messages):
        if (
            not isinstance(authority, GmailTemporalThreadMessageAuthority)
            or authority.version != _MESSAGE_AUTHORITY_VERSION
            or _OPAQUE_KEY.fullmatch(authority.current_analysis_fingerprint) is None
            or _OPAQUE_KEY.fullmatch(authority.current_projection_fingerprint) is None
            or _SHA256_HEX.fullmatch(authority.current_projection_sha256) is None
        ):
            raise GmailTemporalThreadLifecycleError("message authority is invalid")
        if (
            not isinstance(message, GmailTemporalThreadMessageReview)
            or message.version != _MESSAGE_REVIEW_VERSION
            or message.candidate_authorization is not False
            or message.requires_defer is not True
            or message.routable is not False
        ):
            raise GmailTemporalThreadLifecycleError("message review is invalid")
        try:
            authority_source = authority.source.validated()
            review_source = message.source.validated()
        except (TypeError, ValueError) as exc:
            raise GmailTemporalThreadLifecycleError(
                "Gmail source locator is invalid"
            ) from exc
        scope = (
            authority_source.document_id,
            authority_source.document_content_hash,
            authority_source.gmail_account_key,
            authority_source.gmail_thread_id,
            authority_source.gmail_source_revision,
        )
        if base_scope is None:
            base_scope = scope
        elif scope[2:4] != base_scope[2:4]:
            raise GmailTemporalThreadLifecycleError("cross-thread input is forbidden")
        elif scope != base_scope:
            raise GmailTemporalThreadLifecycleError(
                "stale or mixed Gmail source revision is forbidden"
            )
        if review_source != authority_source:
            if (
                review_source.gmail_account_key != authority_source.gmail_account_key
                or review_source.gmail_thread_id != authority_source.gmail_thread_id
            ):
                raise GmailTemporalThreadLifecycleError(
                    "cross-thread input is forbidden"
                )
            raise GmailTemporalThreadLifecycleError(
                "review projection is stale relative to the current source"
            )
        current_run = _opaque(authority.current_review_run_id, "current review run id")
        review_run = _opaque(message.review_run_id, "review run id")
        if current_run != review_run:
            raise GmailTemporalThreadLifecycleError(
                "review projection is not the current append-only ledger head"
            )
        if (
            isinstance(authority.current_head_generation, bool)
            or not isinstance(authority.current_head_generation, int)
            or authority.current_head_generation < 1
        ):
            raise GmailTemporalThreadLifecycleError(
                "current head generation is invalid"
            )
        scope_name = _opaque(authority.pipeline_scope, "pipeline scope")
        if pipeline_scope is None:
            pipeline_scope = scope_name
        elif scope_name != pipeline_scope:
            raise GmailTemporalThreadLifecycleError(
                "mixed pipeline scopes are forbidden"
            )
        if review_run in seen_runs:
            raise GmailTemporalThreadLifecycleError("duplicate review run is forbidden")
        seen_runs.add(review_run)
        if authority_source.gmail_message_id in seen_messages:
            raise GmailTemporalThreadLifecycleError("duplicate message chronology")
        seen_messages.add(authority_source.gmail_message_id)
        order = (
            _parse_aware(authority_source.message_internal_at),
            authority_source.gmail_message_id,
        )
        if previous_order is not None and order <= previous_order:
            raise GmailTemporalThreadLifecycleError(
                "trusted Gmail message chronology is missing or unordered"
            )
        if (
            previous_end is not None
            and authority_source.message_start_offset <= previous_end
        ):
            raise GmailTemporalThreadLifecycleError(
                "trusted Gmail source ranges are not in chronological order"
            )
        previous_order = order
        previous_end = authority_source.message_end_offset

        try:
            payload = gmail_temporal_review_projection_payload(message.projection)
        except GmailTemporalReviewError as exc:
            raise GmailTemporalThreadLifecycleError(
                "message review projection is invalid or stale"
            ) from exc
        if payload.get("source_sha256") != authority_source.source_sha256:
            raise GmailTemporalThreadLifecycleError(
                "message projection source hash is stale"
            )
        if (
            payload.get("analysis_fingerprint")
            != authority.current_analysis_fingerprint
            or payload.get("projection_fingerprint")
            != authority.current_projection_fingerprint
            or hashlib.sha256(_canonical_bytes(payload)).hexdigest()
            != authority.current_projection_sha256
        ):
            raise GmailTemporalThreadLifecycleError(
                "message projection does not match the current ledger receipt"
            )
        _validate_identity_assertions(message)
        normalized_authorities.append(
            GmailTemporalThreadMessageAuthority(
                version="gmail_temporal_thread_message_authority_v2",
                source=authority_source,
                pipeline_scope=scope_name,
                current_review_run_id=current_run,
                current_head_generation=authority.current_head_generation,
                current_analysis_fingerprint=(authority.current_analysis_fingerprint),
                current_projection_fingerprint=(
                    authority.current_projection_fingerprint
                ),
                current_projection_sha256=authority.current_projection_sha256,
            )
        )
        normalized_messages.append(
            GmailTemporalThreadMessageReview(
                version="gmail_temporal_thread_message_review_v1",
                source=review_source,
                review_run_id=review_run,
                projection=message.projection,
                identity_assertions=message.identity_assertions,
            )
        )
    return (
        GmailTemporalThreadSnapshotAuthority(
            version="gmail_temporal_thread_snapshot_authority_v2",
            messages=tuple(normalized_authorities),
            prior_event_identity_resolution_fingerprint=(prior_resolution_fingerprint),
        ),
        tuple(normalized_messages),
    )


def _validate_identity_assertions(message: GmailTemporalThreadMessageReview) -> None:
    if not isinstance(message.identity_assertions, tuple):
        raise GmailTemporalThreadLifecycleError("identity assertions must be immutable")
    targets = {
        (artifact.artifact_id, hypothesis.hypothesis_id): (artifact, hypothesis)
        for artifact in message.projection.artifacts
        for hypothesis in artifact.hypotheses
    }
    source_anchor = {
        "gmail_account_key": message.source.gmail_account_key,
        "gmail_thread_id": message.source.gmail_thread_id,
        "gmail_message_id": message.source.gmail_message_id,
        "source_sha256": message.source.source_sha256,
    }
    seen: set[tuple[str, ...]] = set()
    for item in message.identity_assertions:
        if (
            not isinstance(item, GmailTemporalEventIdentityAssertion)
            or item.version != _IDENTITY_ASSERTION_VERSION
            or item.projection_fingerprint != message.projection.projection_fingerprint
            or item.verification
            not in {
                "unverified",
                "source_bound_self_identity",
                "external_verified",
                "owner_verified",
            }
            or item.candidate_authorization is not False
            or item.requires_defer is not True
            or item.routable is not False
        ):
            raise GmailTemporalThreadLifecycleError(
                "event identity assertion is invalid or stale"
            )
        artifact_id = _opaque(item.artifact_id, "identity artifact id")
        hypothesis_id = _opaque(item.hypothesis_id, "identity hypothesis id")
        unit_id = _opaque(item.unit_id, "event identity unit id")
        event_key = _opaque(item.event_identity_key, "event identity key")
        provenance = _opaque(item.provenance_ref, "identity provenance ref")
        if (artifact_id, hypothesis_id) not in targets:
            raise GmailTemporalThreadLifecycleError(
                "event identity assertion targets an unknown source hypothesis"
            )
        artifact, hypothesis = targets[(artifact_id, hypothesis_id)]
        expected_unit_id = gmail_temporal_event_identity_unit_id(
            source_anchor=source_anchor,
            artifact=artifact,
            hypothesis=hypothesis,
        )
        if unit_id != expected_unit_id:
            raise GmailTemporalThreadLifecycleError(
                "event identity assertion targets the wrong source unit"
            )
        if item.verification == "source_bound_self_identity":
            if event_key != gmail_temporal_source_bound_event_identity_key(
                expected_unit_id
            ) or provenance != gmail_temporal_source_bound_self_provenance(
                expected_unit_id
            ):
                raise GmailTemporalThreadLifecycleError(
                    "source-bound self identity assertion is invalid"
                )
        signature = (
            unit_id,
            artifact_id,
            hypothesis_id,
            event_key,
            item.verification,
            provenance,
        )
        if signature in seen:
            raise GmailTemporalThreadLifecycleError(
                "duplicate event identity assertion is forbidden"
            )
        seen.add(signature)


def _assertions_by_hypothesis(
    message: GmailTemporalThreadMessageReview,
) -> dict[tuple[str, str], tuple[GmailTemporalEventIdentityAssertion, ...]]:
    values: dict[tuple[str, str], list[GmailTemporalEventIdentityAssertion]] = {}
    for item in message.identity_assertions:
        values.setdefault((item.artifact_id, item.hypothesis_id), []).append(item)
    return {
        key: tuple(
            sorted(
                items,
                key=lambda item: (
                    item.unit_id,
                    item.event_identity_key,
                    item.verification,
                    item.provenance_ref,
                ),
            )
        )
        for key, items in values.items()
    }


def _identity_resolution(
    *,
    artifact: GmailTemporalReviewArtifact,
    hypothesis: GmailTemporalReviewHypothesis,
    assertions: Mapping[
        tuple[str, str], tuple[GmailTemporalEventIdentityAssertion, ...]
    ],
) -> _IdentityResolution:
    values = assertions.get((artifact.artifact_id, hypothesis.hypothesis_id), ())
    all_keys = tuple(sorted({item.event_identity_key for item in values}))
    verified = tuple(
        item for item in values if item.verification in _VERIFIED_IDENTITY_AUTHORITIES
    )
    verified_keys = tuple(sorted({item.event_identity_key for item in verified}))
    if not verified_keys:
        return _IdentityResolution(
            key=None,
            asserted_keys=all_keys,
            verified_assertions=(),
            reason=(
                "stable_event_identity_unverified"
                if all_keys
                else "stable_event_identity_missing"
            ),
        )
    if len(verified_keys) != 1:
        return _IdentityResolution(
            key=None,
            asserted_keys=verified_keys,
            verified_assertions=verified,
            reason="conflicting_verified_event_identity",
        )
    return _IdentityResolution(
        key=verified_keys[0],
        asserted_keys=all_keys,
        verified_assertions=verified,
        reason=None,
    )


def _apply_single_group(
    *,
    message: GmailTemporalThreadMessageReview,
    group: GmailTemporalReviewGroup,
    artifacts: Mapping[str, GmailTemporalReviewArtifact],
    assertions: Mapping[
        tuple[str, str], tuple[GmailTemporalEventIdentityAssertion, ...]
    ],
    events: dict[str, _MutableEvent],
    unresolved: list[GmailTemporalLifecycleUnresolvedAlternative],
) -> None:
    artifact_ids = tuple(
        artifact_id for member in group.members for artifact_id in member.artifact_ids
    )
    if len(artifact_ids) != 1:
        refs = _group_source_refs(message, group, artifacts)
        _add_unresolved(
            unresolved,
            events,
            reason="unsupported_single_group_shape",
            keys=_asserted_keys_for_refs(message, refs, assertions),
            statuses=_statuses_for_group(group, artifacts),
            refs=refs,
        )
        return
    artifact = artifacts[artifact_ids[0]]
    refs = tuple(
        _source_ref(
            message,
            source_kind="artifact",
            source_id=artifact.artifact_id,
            hypothesis_id=item.hypothesis_id,
        )
        for item in artifact.hypotheses
    )
    if artifact.kind != "supported_citation" or len(artifact.hypotheses) != 1:
        keys = _asserted_keys_for_artifact(artifact, assertions)
        _add_unresolved(
            unresolved,
            events,
            reason="uncertain_source_artifact",
            keys=keys,
            statuses=_statuses_for_artifact(artifact),
            refs=refs,
        )
        return
    hypothesis = artifact.hypotheses[0]
    identity = _identity_resolution(
        artifact=artifact,
        hypothesis=hypothesis,
        assertions=assertions,
    )
    if identity.key is None:
        _add_unresolved(
            unresolved,
            events,
            reason=identity.reason or "stable_event_identity_missing",
            keys=identity.asserted_keys,
            affect_keys=(
                identity.asserted_keys if identity.verified_assertions else ()
            ),
            statuses=_statuses_for_artifact(artifact),
            refs=refs,
        )
        return
    ref = refs[0]
    if hypothesis.normalized_value is None:
        _add_unresolved(
            unresolved,
            events,
            reason="temporal_value_unresolved",
            keys=(identity.key,),
            statuses=_statuses_for_artifact(artifact),
            refs=(ref,),
        )
        return
    value = _temporal_value(hypothesis.normalized_value)
    if (
        hypothesis.lifecycle == "scheduled"
        and hypothesis.relation in {"occurrence", "scheduled_for"}
        and hypothesis.kind == "planned"
    ):
        _apply_schedule(
            event_key=identity.key,
            value=value,
            ref=ref,
            identity_provenance=identity.verified_assertions,
            events=events,
            unresolved=unresolved,
        )
        return
    if hypothesis.lifecycle in {"cancelled", "completed"}:
        _apply_terminal(
            event_key=identity.key,
            value=value,
            terminal_status=hypothesis.lifecycle,
            ref=ref,
            identity_provenance=identity.verified_assertions,
            events=events,
            unresolved=unresolved,
        )
        return
    _add_unresolved(
        unresolved,
        events,
        reason="unsupported_lifecycle_inference",
        keys=(identity.key,),
        statuses=_statuses_for_artifact(artifact),
        refs=(ref,),
    )


def _apply_reschedule(
    *,
    message: GmailTemporalThreadMessageReview,
    group: GmailTemporalReviewGroup,
    artifacts: Mapping[str, GmailTemporalReviewArtifact],
    assertions: Mapping[
        tuple[str, str], tuple[GmailTemporalEventIdentityAssertion, ...]
    ],
    events: dict[str, _MutableEvent],
    unresolved: list[GmailTemporalLifecycleUnresolvedAlternative],
) -> None:
    by_role = {member.role: member for member in group.members}
    if set(by_role) != {"rescheduled_old", "rescheduled_replacement"}:
        refs = _group_source_refs(message, group, artifacts)
        _add_unresolved(
            unresolved,
            events,
            reason="unsupported_reschedule_shape",
            keys=_asserted_keys_for_refs(message, refs, assertions),
            statuses=("scheduled", "superseded"),
            refs=refs,
        )
        return
    selected: list[
        tuple[
            GmailTemporalReviewArtifact,
            GmailTemporalReviewHypothesis,
            GmailTemporalLifecycleSourceRef,
            _IdentityResolution,
        ]
    ] = []
    for role in ("rescheduled_old", "rescheduled_replacement"):
        member = by_role[role]
        if len(member.artifact_ids) != 1:
            selected = []
            break
        artifact = artifacts[member.artifact_ids[0]]
        if artifact.kind != "supported_citation" or len(artifact.hypotheses) != 1:
            selected = []
            break
        hypothesis = artifact.hypotheses[0]
        selected.append(
            (
                artifact,
                hypothesis,
                _source_ref(
                    message,
                    source_kind="artifact",
                    source_id=artifact.artifact_id,
                    hypothesis_id=hypothesis.hypothesis_id,
                ),
                _identity_resolution(
                    artifact=artifact,
                    hypothesis=hypothesis,
                    assertions=assertions,
                ),
            )
        )
    if len(selected) != 2:
        refs = _group_source_refs(message, group, artifacts)
        _add_unresolved(
            unresolved,
            events,
            reason="uncertain_reschedule_artifact",
            keys=_asserted_keys_for_refs(message, refs, assertions),
            statuses=("scheduled", "superseded"),
            refs=refs,
        )
        return
    keys = tuple(sorted({item[3].key for item in selected if item[3].key}))
    asserted_keys = tuple(
        sorted({key for item in selected for key in item[3].asserted_keys})
    )
    refs = tuple(item[2] for item in selected)
    if any(item[3].key is None for item in selected):
        _add_unresolved(
            unresolved,
            events,
            reason="reschedule_event_identity_unresolved",
            keys=asserted_keys,
            affect_keys=keys,
            statuses=("scheduled", "superseded"),
            refs=refs,
        )
        return
    if len(keys) != 1:
        _add_unresolved(
            unresolved,
            events,
            reason="reschedule_event_identity_mismatch",
            keys=keys,
            statuses=("scheduled", "superseded"),
            refs=refs,
        )
        return
    if any(item[1].normalized_value is None for item in selected):
        _add_unresolved(
            unresolved,
            events,
            reason="reschedule_temporal_value_unresolved",
            keys=keys,
            statuses=("scheduled", "superseded"),
            refs=refs,
        )
        return
    old_value = _temporal_value(selected[0][1].normalized_value or "")
    replacement_value = _temporal_value(selected[1][1].normalized_value or "")
    event_key = keys[0]
    event = events.get(event_key)
    if (
        event is None
        or event.ambiguous
        or event.current_status != "scheduled"
        or event.current_occurrence_id is None
    ):
        _add_unresolved(
            unresolved,
            events,
            reason="reschedule_old_occurrence_not_current",
            keys=(event_key,),
            statuses=("scheduled", "superseded"),
            refs=refs,
        )
        return
    current = _current_occurrence(event)
    if current.normalized_value != old_value or old_value == replacement_value:
        _add_unresolved(
            unresolved,
            events,
            reason=(
                "reschedule_repeats_same_occurrence"
                if old_value == replacement_value
                else "reschedule_old_occurrence_not_current"
            ),
            keys=(event_key,),
            statuses=("scheduled", "superseded"),
            refs=refs,
        )
        return
    _extend_unique_refs(current.source_refs, (selected[0][2],))
    occurrence_id = _occurrence_id(
        event_key=event_key,
        normalized_value=replacement_value,
        source_ref=selected[1][2],
    )
    current.state = "superseded"
    current.superseded_by_occurrence_id = occurrence_id
    event.occurrences.append(
        _MutableOccurrence(
            occurrence_id=occurrence_id,
            normalized_value=replacement_value,
            state="scheduled",
            superseded_by_occurrence_id=None,
            source_refs=[selected[1][2]],
        )
    )
    event.current_occurrence_id = occurrence_id
    _extend_unique_assertions(
        event.identity_provenance,
        tuple(
            item
            for selected_item in selected
            for item in selected_item[3].verified_assertions
        ),
    )


def _apply_schedule(
    *,
    event_key: str,
    value: str,
    ref: GmailTemporalLifecycleSourceRef,
    identity_provenance: tuple[GmailTemporalEventIdentityAssertion, ...],
    events: dict[str, _MutableEvent],
    unresolved: list[GmailTemporalLifecycleUnresolvedAlternative],
) -> None:
    event = events.get(event_key)
    if event is None:
        occurrence_id = _occurrence_id(
            event_key=event_key,
            normalized_value=value,
            source_ref=ref,
        )
        events[event_key] = _MutableEvent(
            event_identity_key=event_key,
            current_status="scheduled",
            current_occurrence_id=occurrence_id,
            occurrences=[
                _MutableOccurrence(
                    occurrence_id=occurrence_id,
                    normalized_value=value,
                    state="scheduled",
                    superseded_by_occurrence_id=None,
                    source_refs=[ref],
                )
            ],
            identity_provenance=list(identity_provenance),
        )
        return
    _extend_unique_assertions(event.identity_provenance, identity_provenance)
    if event.ambiguous:
        _add_unresolved(
            unresolved,
            events,
            reason="prior_event_state_unresolved",
            keys=(event_key,),
            statuses=("scheduled",),
            refs=(ref,),
        )
        return
    if event.current_status == "scheduled" and event.current_occurrence_id:
        current = _current_occurrence(event)
        if current.normalized_value == value:
            _extend_unique_refs(current.source_refs, (ref,))
            return
        _add_unresolved(
            unresolved,
            events,
            reason="schedule_change_requires_explicit_reschedule",
            keys=(event_key,),
            statuses=("scheduled",),
            refs=(ref,),
        )
        return
    _add_unresolved(
        unresolved,
        events,
        reason="terminal_event_reopened_without_explicit_evidence",
        keys=(event_key,),
        statuses=("scheduled",),
        refs=(ref,),
    )


def _apply_terminal(
    *,
    event_key: str,
    value: str,
    terminal_status: Literal["cancelled", "completed"],
    ref: GmailTemporalLifecycleSourceRef,
    identity_provenance: tuple[GmailTemporalEventIdentityAssertion, ...],
    events: dict[str, _MutableEvent],
    unresolved: list[GmailTemporalLifecycleUnresolvedAlternative],
) -> None:
    event = events.get(event_key)
    if event is None:
        event = _MutableEvent(
            event_identity_key=event_key,
            current_status=None,
            current_occurrence_id=None,
            occurrences=[],
            identity_provenance=list(identity_provenance),
        )
        events[event_key] = event
    else:
        _extend_unique_assertions(event.identity_provenance, identity_provenance)
    if event.ambiguous:
        _add_unresolved(
            unresolved,
            events,
            reason="prior_event_state_unresolved",
            keys=(event_key,),
            statuses=(terminal_status,),
            refs=(ref,),
        )
        return
    if event.current_status == terminal_status and event.current_occurrence_id:
        current = _current_occurrence(event)
        if current.normalized_value == value:
            _extend_unique_refs(current.source_refs, (ref,))
            return
    if event.current_status != "scheduled" or event.current_occurrence_id is None:
        _add_unresolved(
            unresolved,
            events,
            reason="terminal_transition_lacks_current_scheduled_occurrence",
            keys=(event_key,),
            statuses=(terminal_status,),
            refs=(ref,),
        )
        return
    current = _current_occurrence(event)
    if current.normalized_value != value:
        _add_unresolved(
            unresolved,
            events,
            reason="terminal_occurrence_not_current",
            keys=(event_key,),
            statuses=(terminal_status,),
            refs=(ref,),
        )
        return
    current.state = terminal_status
    _extend_unique_refs(current.source_refs, (ref,))
    event.current_status = terminal_status


def _add_unresolved(
    unresolved: list[GmailTemporalLifecycleUnresolvedAlternative],
    events: Mapping[str, _MutableEvent],
    *,
    reason: str,
    keys: tuple[str, ...],
    affect_keys: tuple[str, ...] | None = None,
    statuses: tuple[str, ...],
    refs: tuple[GmailTemporalLifecycleSourceRef, ...],
) -> None:
    normalized_keys = tuple(sorted(set(keys)))
    normalized_statuses = tuple(sorted(set(statuses or ("unresolved",))))
    normalized_refs = tuple(sorted(set(refs), key=lambda item: item.ref_id))
    material = {
        "version": _UNRESOLVED_VERSION,
        "reason": reason,
        "possible_event_identity_keys": normalized_keys,
        "possible_statuses": normalized_statuses,
        "source_ref_ids": [item.ref_id for item in normalized_refs],
    }
    unresolved.append(
        GmailTemporalLifecycleUnresolvedAlternative(
            version="gmail_temporal_lifecycle_unresolved_alternative_v1",
            alternative_id=(
                "gtlua_" + hashlib.sha256(_canonical_bytes(material)).hexdigest()
            ),
            reason=reason,
            possible_event_identity_keys=normalized_keys,
            possible_statuses=normalized_statuses,
            source_refs=normalized_refs,
        )
    )
    state_keys = normalized_keys if affect_keys is None else tuple(set(affect_keys))
    for key in state_keys:
        if key in events:
            events[key].ambiguous = True


def _freeze_event(event: _MutableEvent) -> GmailTemporalEventLifecycleView:
    occurrences = tuple(
        GmailTemporalLifecycleOccurrence(
            version="gmail_temporal_lifecycle_occurrence_v1",
            occurrence_id=item.occurrence_id,
            normalized_value=item.normalized_value,
            state=item.state,
            superseded_by_occurrence_id=item.superseded_by_occurrence_id,
            source_refs=tuple(item.source_refs),
        )
        for item in event.occurrences
    )
    provenance = tuple(
        sorted(
            event.identity_provenance,
            key=lambda item: (
                item.unit_id,
                item.projection_fingerprint,
                item.artifact_id,
                item.hypothesis_id,
                item.verification,
                item.provenance_ref,
            ),
        )
    )
    return GmailTemporalEventLifecycleView(
        version="gmail_temporal_event_lifecycle_view_v1",
        event_identity_key=event.event_identity_key,
        entity_type="event",
        current_status=(
            "unresolved"
            if event.ambiguous or event.current_status is None
            else event.current_status
        ),
        last_unambiguous_status=event.current_status,
        current_occurrence_id=event.current_occurrence_id,
        occurrences=occurrences,
        identity_provenance=provenance,
    )


def _group_source_refs(
    message: GmailTemporalThreadMessageReview,
    group: GmailTemporalReviewGroup,
    artifacts: Mapping[str, GmailTemporalReviewArtifact],
) -> tuple[GmailTemporalLifecycleSourceRef, ...]:
    refs: list[GmailTemporalLifecycleSourceRef] = []
    for member in group.members:
        for artifact_id in member.artifact_ids:
            artifact = artifacts[artifact_id]
            refs.extend(
                _source_ref(
                    message,
                    source_kind="artifact",
                    source_id=artifact.artifact_id,
                    hypothesis_id=hypothesis.hypothesis_id,
                )
                for hypothesis in artifact.hypotheses
            )
        for review_id in member.cluster_review_ids:
            refs.append(
                _source_ref(
                    message,
                    source_kind="cluster_review",
                    source_id=review_id,
                    hypothesis_id=None,
                )
            )
    if not refs:
        refs.append(
            _source_ref(
                message,
                source_kind="group",
                source_id=group.group_id,
                hypothesis_id=None,
            )
        )
    return tuple(refs)


def _source_ref(
    message: GmailTemporalThreadMessageReview,
    *,
    source_kind: LifecycleSourceKind,
    source_id: str,
    hypothesis_id: str | None,
) -> GmailTemporalLifecycleSourceRef:
    material = {
        "version": _SOURCE_REF_VERSION,
        "gmail_message_id": message.source.gmail_message_id,
        "review_run_id": message.review_run_id,
        "projection_fingerprint": message.projection.projection_fingerprint,
        "source_kind": source_kind,
        "source_id": source_id,
        "hypothesis_id": hypothesis_id,
    }
    return GmailTemporalLifecycleSourceRef(
        version="gmail_temporal_lifecycle_source_ref_v1",
        ref_id="gtlsr_" + hashlib.sha256(_canonical_bytes(material)).hexdigest(),
        gmail_message_id=message.source.gmail_message_id,
        review_run_id=message.review_run_id,
        projection_fingerprint=message.projection.projection_fingerprint,
        source_kind=source_kind,
        source_id=source_id,
        hypothesis_id=hypothesis_id,
    )


def _asserted_keys_for_artifact(
    artifact: GmailTemporalReviewArtifact,
    assertions: Mapping[
        tuple[str, str], tuple[GmailTemporalEventIdentityAssertion, ...]
    ],
) -> tuple[str, ...]:
    return tuple(
        sorted(
            {
                item.event_identity_key
                for hypothesis in artifact.hypotheses
                for item in assertions.get(
                    (artifact.artifact_id, hypothesis.hypothesis_id), ()
                )
                if item.verification in _VERIFIED_IDENTITY_AUTHORITIES
            }
        )
    )


def _asserted_keys_for_refs(
    message: GmailTemporalThreadMessageReview,
    refs: tuple[GmailTemporalLifecycleSourceRef, ...],
    assertions: Mapping[
        tuple[str, str], tuple[GmailTemporalEventIdentityAssertion, ...]
    ],
) -> tuple[str, ...]:
    del message
    keys: set[str] = set()
    for ref in refs:
        if ref.source_kind != "artifact" or ref.hypothesis_id is None:
            continue
        keys.update(
            item.event_identity_key
            for item in assertions.get((ref.source_id, ref.hypothesis_id), ())
            if item.verification in _VERIFIED_IDENTITY_AUTHORITIES
        )
    return tuple(sorted(keys))


def _statuses_for_artifact(
    artifact: GmailTemporalReviewArtifact,
) -> tuple[str, ...]:
    values = {
        item.lifecycle
        for item in artifact.hypotheses
        if item.lifecycle in {"scheduled", "cancelled", "completed"}
    }
    return tuple(sorted(values or {"unresolved"}))


def _statuses_for_group(
    group: GmailTemporalReviewGroup,
    artifacts: Mapping[str, GmailTemporalReviewArtifact],
) -> tuple[str, ...]:
    values: set[str] = set()
    for member in group.members:
        for artifact_id in member.artifact_ids:
            values.update(_statuses_for_artifact(artifacts[artifact_id]))
    if group.kind == "reschedule":
        values.update({"scheduled", "superseded"})
    return tuple(sorted(values or {"unresolved"}))


def _occurrence_id(
    *,
    event_key: str,
    normalized_value: str,
    source_ref: GmailTemporalLifecycleSourceRef,
) -> str:
    material = {
        "version": _OCCURRENCE_VERSION,
        "event_identity_key": event_key,
        "normalized_value": normalized_value,
        "origin_source_ref_id": source_ref.ref_id,
    }
    return "gtlo_" + hashlib.sha256(_canonical_bytes(material)).hexdigest()


def _current_occurrence(event: _MutableEvent) -> _MutableOccurrence:
    for item in event.occurrences:
        if item.occurrence_id == event.current_occurrence_id:
            return item
    raise GmailTemporalThreadLifecycleError("current occurrence reference is stale")


def _extend_unique_refs(
    target: list[GmailTemporalLifecycleSourceRef],
    values: tuple[GmailTemporalLifecycleSourceRef, ...],
) -> None:
    seen = {item.ref_id for item in target}
    for item in values:
        if item.ref_id not in seen:
            target.append(item)
            seen.add(item.ref_id)


def _extend_unique_assertions(
    target: list[GmailTemporalEventIdentityAssertion],
    values: tuple[GmailTemporalEventIdentityAssertion, ...],
) -> None:
    seen = {
        (
            item.unit_id,
            item.projection_fingerprint,
            item.artifact_id,
            item.hypothesis_id,
            item.event_identity_key,
            item.verification,
            item.provenance_ref,
        )
        for item in target
    }
    for item in values:
        signature = (
            item.unit_id,
            item.projection_fingerprint,
            item.artifact_id,
            item.hypothesis_id,
            item.event_identity_key,
            item.verification,
            item.provenance_ref,
        )
        if signature not in seen:
            target.append(item)
            seen.add(signature)


def _opaque(value: Any, name: str) -> str:
    if not isinstance(value, str) or _OPAQUE_KEY.fullmatch(value) is None:
        raise GmailTemporalThreadLifecycleError(f"{name} is invalid")
    return value


def _temporal_value(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > 512:
        raise GmailTemporalThreadLifecycleError("normalized temporal value is invalid")
    return value


def _parse_aware(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise GmailTemporalThreadLifecycleError(
            "trusted Gmail message chronology is invalid"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise GmailTemporalThreadLifecycleError(
            "trusted Gmail message chronology is missing"
        )
    return parsed.astimezone(timezone.utc)


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
