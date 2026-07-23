from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, replace
from itertools import combinations
from typing import Any, Literal, Mapping

from .gmail_temporal_leads import (
    TemporalLeadAnalysis,
    validate_gmail_temporal_lead_analysis_authority,
)
from .gmail_temporal_persistence import gmail_temporal_message_scope_key
from .gmail_temporal_review import (
    GmailTemporalReviewArtifact,
    GmailTemporalReviewHypothesis,
)
from .gmail_temporal_selection import GMAIL_TEMPORAL_SUBJECT_TYPES
from .gmail_temporal_thread_lifecycle import (
    GmailTemporalEventIdentityAssertion,
    GmailTemporalThreadMessageReview,
    GmailTemporalThreadSnapshotAuthority,
    gmail_temporal_event_identity_unit_id,
    gmail_temporal_source_bound_event_identity_key,
    gmail_temporal_source_bound_self_provenance,
    validate_gmail_temporal_thread_review_inputs,
)


_UNIT_VERSION = "gmail_temporal_event_identity_unit_v2"
_UNIT_AUTHORITY_VERSION = "gmail_temporal_event_identity_unit_authority_v2"
_PAIR_VERSION = "gmail_temporal_event_identity_pair_v2"
_PAGE_VERSION = "gmail_temporal_event_identity_page_v2"
_PLAN_VERSION = "gmail_temporal_event_identity_plan_v2"
_VERDICT_SET_VERSION = "gmail_temporal_event_identity_verdict_set_v2"
_PAIR_CONSENSUS_VERSION = "gmail_temporal_event_identity_pair_consensus_v2"
_CLUSTER_VERSION = "gmail_temporal_event_identity_cluster_v2"
_REVIEW_VERSION = "gmail_temporal_event_identity_review_v2"
_RESOLUTION_VERSION = "gmail_temporal_event_identity_resolution_v2"

MAX_EVENT_IDENTITY_UNITS = 64
MAX_EVENT_IDENTITY_PAIRS = 2016
DEFAULT_PAIRS_PER_PAGE = 16
MAX_PAIRS_PER_PAGE = 32

_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")
_OPAQUE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,511}$")

IdentityVerdict = Literal["same_event", "different_event", "uncertain"]
IdentityConsensus = Literal["same_event", "different_event", "uncertain"]

_EVENT_CENTERED_SUBJECT_TYPES = frozenset(
    {"event", "event_title_candidate", "event_predicate"}
)


class GmailTemporalEventIdentityError(ValueError):
    """Raised when event identity authority or verdict coverage fails closed."""


@dataclass(frozen=True)
class GmailTemporalEventIdentityUnit:
    """One exact artifact/hypothesis bound to immutable message authority."""

    version: Literal["gmail_temporal_event_identity_unit_v2"]
    unit_id: str
    message_order: int
    message_authority_fingerprint: str
    unit_authority_fingerprint: str
    projection_fingerprint: str
    artifact_id: str
    artifact_kind: str
    evidence_status: str
    hypothesis_id: str
    subject_mention_ids: tuple[str, ...]
    subject_type_references: tuple[tuple[str, str], ...]
    relation: str
    kind: str
    lifecycle: str
    normalized_value: str | None
    candidate_requires_defer: bool
    candidate_authorization: Literal[False] = False
    requires_defer: Literal[True] = True
    routable: Literal[False] = False


@dataclass(frozen=True)
class GmailTemporalEventIdentityPair:
    version: Literal["gmail_temporal_event_identity_pair_v2"]
    pair_id: str
    left_unit_id: str
    right_unit_id: str
    candidate_authorization: Literal[False] = False
    requires_defer: Literal[True] = True
    routable: Literal[False] = False


@dataclass(frozen=True)
class GmailTemporalEventIdentityPage:
    version: Literal["gmail_temporal_event_identity_page_v2"]
    sequence: int
    page_fingerprint: str
    pair_ids: tuple[str, ...]
    candidate_authorization: Literal[False] = False
    requires_defer: Literal[True] = True
    routable: Literal[False] = False


@dataclass(frozen=True)
class GmailTemporalEventIdentityPlan:
    """Complete bounded cross-unit comparison authority."""

    version: Literal["gmail_temporal_event_identity_plan_v2"]
    plan_fingerprint: str
    thread_authority_fingerprint: str
    authorized_prior_resolution_fingerprint: str | None
    units: tuple[GmailTemporalEventIdentityUnit, ...]
    pairs: tuple[GmailTemporalEventIdentityPair, ...]
    pages: tuple[GmailTemporalEventIdentityPage, ...]
    max_units: int
    max_pairs: int
    pairs_per_page: int
    complete: Literal[True]
    candidate_authorization: Literal[False] = False
    requires_defer: Literal[True] = True
    routable: Literal[False] = False


@dataclass(frozen=True)
class GmailTemporalEventIdentityPairVerdict:
    pair_id: str
    verdict: IdentityVerdict
    routable: Literal[False] = False


@dataclass(frozen=True)
class GmailTemporalEventIdentityVerdictSet:
    """One exact external response over every pair in one immutable plan."""

    version: Literal["gmail_temporal_event_identity_verdict_set_v2"]
    verdict_set_fingerprint: str
    plan_fingerprint: str
    run_ordinal: int
    invocation_id: str
    response_sha256: str
    verdicts: tuple[GmailTemporalEventIdentityPairVerdict, ...]
    independent_invocation_verified: Literal[False] = False
    candidate_authorization: Literal[False] = False
    requires_defer: Literal[True] = True
    routable: Literal[False] = False


@dataclass(frozen=True)
class GmailTemporalEventIdentityPairConsensus:
    version: Literal["gmail_temporal_event_identity_pair_consensus_v2"]
    pair_id: str
    consensus: IdentityConsensus
    run_verdicts: tuple[IdentityVerdict, ...]
    candidate_authorization: Literal[False] = False
    requires_defer: Literal[True] = True
    routable: Literal[False] = False


@dataclass(frozen=True)
class GmailTemporalEventIdentityCluster:
    version: Literal["gmail_temporal_event_identity_cluster_v2"]
    cluster_id: str
    event_identity_key: str
    event_identity_anchor_unit_id: str
    unit_ids: tuple[str, ...]
    supporting_pair_ids: tuple[str, ...]
    provenance_ref: str
    candidate_authorization: Literal[False] = False
    requires_defer: Literal[True] = True
    routable: Literal[False] = False


@dataclass(frozen=True)
class GmailTemporalEventIdentityReview:
    version: Literal["gmail_temporal_event_identity_review_v2"]
    review_id: str
    reason: Literal["non_clique_or_contradictory_same_event_component"]
    unit_ids: tuple[str, ...]
    pair_ids: tuple[str, ...]
    candidate_authorization: Literal[False] = False
    requires_defer: Literal[True] = True
    routable: Literal[False] = False


@dataclass(frozen=True)
class GmailTemporalEventIdentityResolution:
    """Self identity or three-run consensus projected into lifecycle assertions."""

    version: Literal["gmail_temporal_event_identity_resolution_v2"]
    resolution_fingerprint: str
    plan_fingerprint: str
    prior_resolution_fingerprint: str | None
    unit_ids: tuple[str, ...]
    verdict_set_fingerprints: tuple[str, ...]
    pair_consensus: tuple[GmailTemporalEventIdentityPairConsensus, ...]
    clusters: tuple[GmailTemporalEventIdentityCluster, ...]
    reviews: tuple[GmailTemporalEventIdentityReview, ...]
    assertions: tuple[GmailTemporalEventIdentityAssertion, ...]
    complete: Literal[True]
    independent_invocations_verified: Literal[False] = False
    candidate_authorization: Literal[False] = False
    requires_defer: Literal[True] = True
    routable: Literal[False] = False


def _bind_analysis_authorities(
    *,
    snapshot_authority: GmailTemporalThreadSnapshotAuthority,
    messages: tuple[GmailTemporalThreadMessageReview, ...],
    analysis_authorities: tuple[TemporalLeadAnalysis, ...],
) -> tuple[dict[str, str], ...]:
    """Bind projection subject types back to trusted, content-free analyses."""

    if (
        not isinstance(analysis_authorities, tuple)
        or len(analysis_authorities) != len(messages)
        or any(
            not isinstance(analysis, TemporalLeadAnalysis)
            for analysis in analysis_authorities
        )
    ):
        raise GmailTemporalEventIdentityError(
            "identity analysis authority coverage is incomplete"
        )

    bound: list[dict[str, str]] = []
    for message_authority, message, analysis in zip(
        snapshot_authority.messages,
        messages,
        analysis_authorities,
        strict=True,
    ):
        chunk_id = gmail_temporal_message_scope_key(
            gmail_account_key=message.source.gmail_account_key,
            gmail_thread_id=message.source.gmail_thread_id,
            gmail_message_id=message.source.gmail_message_id,
        )
        if (
            analysis.version != "gmail_temporal_leads_v2"
            or analysis.snapshot_fingerprint != message.projection.analysis_fingerprint
            or analysis.snapshot_fingerprint
            != message_authority.current_analysis_fingerprint
            or analysis.source_sha256 != message.projection.source_sha256
            or analysis.source_sha256 != message.source.source_sha256
        ):
            raise GmailTemporalEventIdentityError(
                "identity analysis authority is stale or mismatched"
            )
        try:
            validate_gmail_temporal_lead_analysis_authority(
                analysis,
                expected_snapshot_fingerprint=(
                    message_authority.current_analysis_fingerprint
                ),
                source_sha256=message.source.source_sha256,
                message_internal_at=message.source.message_internal_at,
                chunk_id=chunk_id,
            )
        except ValueError as exc:
            raise GmailTemporalEventIdentityError(
                "identity analysis authority is stale or mismatched"
            ) from exc
        subject_types = {
            mention.mention_id: mention.mention_type for mention in analysis.mentions
        }
        if len(subject_types) != len(analysis.mentions):
            raise GmailTemporalEventIdentityError(
                "identity analysis subject authority is ambiguous"
            )
        expression_ids = {
            expression.expression_id for expression in analysis.expressions
        }
        mention_ids = set(subject_types)
        for artifact in message.projection.artifacts:
            for hypothesis in artifact.hypotheses:
                if (
                    hypothesis.expression_id not in expression_ids
                    or not set(hypothesis.lifecycle_mention_ids).issubset(mention_ids)
                    or not set(hypothesis.subject_mention_ids).issubset(mention_ids)
                ):
                    raise GmailTemporalEventIdentityError(
                        "identity projection exceeds its analysis authority"
                    )
                expected_references = tuple(
                    (mention_id, subject_types[mention_id])
                    for mention_id in hypothesis.subject_mention_ids
                )
                if (
                    any(
                        mention_type not in GMAIL_TEMPORAL_SUBJECT_TYPES
                        for _, mention_type in expected_references
                    )
                    or hypothesis.subject_type_references != expected_references
                ):
                    raise GmailTemporalEventIdentityError(
                        "identity projection subject types do not match analysis authority"
                    )
        bound.append(subject_types)
    return tuple(bound)


def plan_gmail_temporal_event_identity(
    *,
    snapshot_authority: GmailTemporalThreadSnapshotAuthority,
    messages: tuple[GmailTemporalThreadMessageReview, ...],
    analysis_authorities: tuple[TemporalLeadAnalysis, ...],
    pairs_per_page: int = DEFAULT_PAIRS_PER_PAGE,
) -> GmailTemporalEventIdentityPlan:
    """Enumerate every bounded cross-unit pair from trusted analysis authority."""

    if (
        isinstance(pairs_per_page, bool)
        or not isinstance(pairs_per_page, int)
        or not 1 <= pairs_per_page <= MAX_PAIRS_PER_PAGE
    ):
        raise GmailTemporalEventIdentityError("identity page bound is invalid")
    try:
        authority, normalized_messages = validate_gmail_temporal_thread_review_inputs(
            snapshot_authority=snapshot_authority,
            messages=messages,
        )
    except ValueError as exc:
        raise GmailTemporalEventIdentityError(
            "thread review authority is stale or invalid"
        ) from exc
    subject_types_by_message = _bind_analysis_authorities(
        snapshot_authority=authority,
        messages=normalized_messages,
        analysis_authorities=analysis_authorities,
    )
    authority_material = {
        "snapshot_authority": asdict(authority),
        "messages": [
            {
                "source": asdict(message.source),
                "review_run_id": message.review_run_id,
                "projection_fingerprint": message.projection.projection_fingerprint,
                "analysis_authority": asdict(analysis_authorities[index]),
            }
            for index, message in enumerate(normalized_messages)
        ],
    }
    authority_fingerprint = (
        "gteia_" + hashlib.sha256(_canonical_bytes(authority_material)).hexdigest()
    )

    units: list[GmailTemporalEventIdentityUnit] = []
    for message_order, message in enumerate(normalized_messages, start=1):
        message_authority = {
            "authority": asdict(authority.messages[message_order - 1]),
            "source": asdict(message.source),
            "review_run_id": message.review_run_id,
            "projection_fingerprint": message.projection.projection_fingerprint,
            "analysis_authority": asdict(analysis_authorities[message_order - 1]),
        }
        message_fingerprint = (
            "gteim_" + hashlib.sha256(_canonical_bytes(message_authority)).hexdigest()
        )
        structural_event_artifact_ids = {
            artifact_id
            for group in message.projection.groups
            if group.kind == "reschedule" and group.coverage == "complete"
            for member in group.members
            for artifact_id in member.artifact_ids
        }
        authoritative_subject_types = subject_types_by_message[message_order - 1]
        for artifact in message.projection.artifacts:
            for hypothesis in artifact.hypotheses:
                subject_type_references = tuple(
                    (mention_id, authoritative_subject_types[mention_id])
                    for mention_id in hypothesis.subject_mention_ids
                )
                if not _is_event_identity_hypothesis(
                    artifact_id=artifact.artifact_id,
                    hypothesis=hypothesis,
                    subject_type_references=subject_type_references,
                    structural_event_artifact_ids=structural_event_artifact_ids,
                ):
                    continue
                unit = _event_unit(
                    message_order=message_order,
                    message_authority_fingerprint=message_fingerprint,
                    source_anchor={
                        "gmail_account_key": message.source.gmail_account_key,
                        "gmail_thread_id": message.source.gmail_thread_id,
                        "gmail_message_id": message.source.gmail_message_id,
                        "source_sha256": message.source.source_sha256,
                    },
                    projection_fingerprint=message.projection.projection_fingerprint,
                    artifact=artifact,
                    hypothesis=hypothesis,
                    subject_type_references=subject_type_references,
                )
                units.append(unit)
    if len(units) > MAX_EVENT_IDENTITY_UNITS:
        raise GmailTemporalEventIdentityError("identity unit bound exceeded")
    if len({item.unit_id for item in units}) != len(units):
        raise GmailTemporalEventIdentityError("identity units are duplicated")
    units.sort(key=lambda item: (item.message_order, item.unit_id))

    pairs = tuple(
        sorted(
            (_event_pair(left, right) for left, right in combinations(units, 2)),
            key=lambda item: item.pair_id,
        )
    )
    if len(pairs) > MAX_EVENT_IDENTITY_PAIRS:
        raise GmailTemporalEventIdentityError("identity comparison bound exceeded")
    pages = tuple(
        _event_page(sequence, pairs[offset : offset + pairs_per_page])
        for sequence, offset in enumerate(range(0, len(pairs), pairs_per_page), start=1)
    )
    material = {
        "version": _PLAN_VERSION,
        "thread_authority_fingerprint": authority_fingerprint,
        "authorized_prior_resolution_fingerprint": (
            authority.prior_event_identity_resolution_fingerprint
        ),
        "units": [asdict(item) for item in units],
        "pairs": [asdict(item) for item in pairs],
        "pages": [asdict(item) for item in pages],
        "max_units": MAX_EVENT_IDENTITY_UNITS,
        "max_pairs": MAX_EVENT_IDENTITY_PAIRS,
        "pairs_per_page": pairs_per_page,
        "complete": True,
        "candidate_authorization": False,
        "requires_defer": True,
        "routable": False,
    }
    plan = GmailTemporalEventIdentityPlan(
        version="gmail_temporal_event_identity_plan_v2",
        plan_fingerprint="gteip_"
        + hashlib.sha256(_canonical_bytes(material)).hexdigest(),
        thread_authority_fingerprint=authority_fingerprint,
        authorized_prior_resolution_fingerprint=(
            authority.prior_event_identity_resolution_fingerprint
        ),
        units=tuple(units),
        pairs=pairs,
        pages=pages,
        max_units=MAX_EVENT_IDENTITY_UNITS,
        max_pairs=MAX_EVENT_IDENTITY_PAIRS,
        pairs_per_page=pairs_per_page,
        complete=True,
    )
    _validate_plan(plan)
    return plan


def make_gmail_temporal_event_identity_verdict_set(
    *,
    plan: GmailTemporalEventIdentityPlan,
    run_ordinal: int,
    invocation_id: str,
    response_sha256: str,
    verdicts: Mapping[str, IdentityVerdict],
) -> GmailTemporalEventIdentityVerdictSet:
    """Bind one complete external verdict mapping to the exact plan."""

    _validate_plan(plan)
    if run_ordinal not in {1, 2, 3} or isinstance(run_ordinal, bool):
        raise GmailTemporalEventIdentityError("identity run ordinal is invalid")
    invocation = _opaque(invocation_id, "identity invocation id")
    response_hash = _sha256(response_sha256, "identity response hash")
    expected_pair_ids = tuple(item.pair_id for item in plan.pairs)
    if not isinstance(verdicts, Mapping) or set(verdicts) != set(expected_pair_ids):
        raise GmailTemporalEventIdentityError("identity verdict coverage is incomplete")
    rows = tuple(
        GmailTemporalEventIdentityPairVerdict(
            pair_id=pair_id,
            verdict=_verdict(verdicts[pair_id]),
        )
        for pair_id in expected_pair_ids
    )
    material = {
        "version": _VERDICT_SET_VERSION,
        "plan_fingerprint": plan.plan_fingerprint,
        "run_ordinal": run_ordinal,
        "invocation_id": invocation,
        "response_sha256": response_hash,
        "verdicts": [asdict(item) for item in rows],
        "independent_invocation_verified": False,
        "candidate_authorization": False,
        "requires_defer": True,
        "routable": False,
    }
    result = GmailTemporalEventIdentityVerdictSet(
        version="gmail_temporal_event_identity_verdict_set_v2",
        verdict_set_fingerprint=(
            "gteivs_" + hashlib.sha256(_canonical_bytes(material)).hexdigest()
        ),
        plan_fingerprint=plan.plan_fingerprint,
        run_ordinal=run_ordinal,
        invocation_id=invocation,
        response_sha256=response_hash,
        verdicts=rows,
    )
    _validate_verdict_set(plan, result)
    return result


def resolve_gmail_temporal_event_identity(
    *,
    plan: GmailTemporalEventIdentityPlan,
    verdict_sets: tuple[GmailTemporalEventIdentityVerdictSet, ...],
    prior_resolution: GmailTemporalEventIdentityResolution | None = None,
) -> GmailTemporalEventIdentityResolution:
    """Resolve source-bound singletons or unanimous cross-unit cliques."""

    _validate_plan(plan)
    supplied_prior_fingerprint = (
        prior_resolution.resolution_fingerprint
        if isinstance(prior_resolution, GmailTemporalEventIdentityResolution)
        else None
    )
    if supplied_prior_fingerprint != plan.authorized_prior_resolution_fingerprint:
        raise GmailTemporalEventIdentityError(
            "identity prior resolution does not match the trusted thread authority"
        )
    if prior_resolution is not None:
        _validate_resolution(prior_resolution)
        if not set(prior_resolution.unit_ids).issubset(
            {item.unit_id for item in plan.units}
        ):
            raise GmailTemporalEventIdentityError(
                "prior identity units were dropped or mutated"
            )
    if not isinstance(verdict_sets, tuple):
        raise GmailTemporalEventIdentityError("identity verdict sets are invalid")
    if not plan.pairs:
        if verdict_sets:
            raise GmailTemporalEventIdentityError(
                "zero verdict sets are required when no identity pairs exist"
            )
    elif len(verdict_sets) != 3:
        raise GmailTemporalEventIdentityError("exactly three verdict sets are required")
    for verdict_set in verdict_sets:
        _validate_verdict_set(plan, verdict_set)
    by_ordinal = {item.run_ordinal: item for item in verdict_sets}
    if plan.pairs and set(by_ordinal) != {1, 2, 3}:
        raise GmailTemporalEventIdentityError("identity run ordinals are incomplete")
    ordered_sets = tuple(by_ordinal[index] for index in (1, 2, 3)) if plan.pairs else ()
    if plan.pairs and len({item.invocation_id for item in ordered_sets}) != 3:
        raise GmailTemporalEventIdentityError("identity invocations are not distinct")

    run_maps = [
        {item.pair_id: item.verdict for item in verdict_set.verdicts}
        for verdict_set in ordered_sets
    ]
    pair_consensus: list[GmailTemporalEventIdentityPairConsensus] = []
    consensus_by_pair: dict[str, IdentityConsensus] = {}
    for pair in plan.pairs:
        values = tuple(run[pair.pair_id] for run in run_maps)
        consensus: IdentityConsensus = (
            values[0] if len(set(values)) == 1 else "uncertain"
        )
        consensus_by_pair[pair.pair_id] = consensus
        pair_consensus.append(
            GmailTemporalEventIdentityPairConsensus(
                version="gmail_temporal_event_identity_pair_consensus_v2",
                pair_id=pair.pair_id,
                consensus=consensus,
                run_verdicts=values,
            )
        )

    units = {item.unit_id: item for item in plan.units}
    pairs_by_units = {
        frozenset((item.left_unit_id, item.right_unit_id)): item for item in plan.pairs
    }
    adjacency: dict[str, set[str]] = {unit_id: set() for unit_id in units}
    for pair in plan.pairs:
        if consensus_by_pair[pair.pair_id] == "same_event":
            adjacency[pair.left_unit_id].add(pair.right_unit_id)
            adjacency[pair.right_unit_id].add(pair.left_unit_id)

    clusters: list[GmailTemporalEventIdentityCluster] = []
    reviews: list[GmailTemporalEventIdentityReview] = []
    assertions: list[GmailTemporalEventIdentityAssertion] = []
    visited: set[str] = set()
    verdict_fingerprints = tuple(item.verdict_set_fingerprint for item in ordered_sets)
    for unit_id in sorted(units):
        if unit_id in visited:
            continue
        if not adjacency[unit_id]:
            component_ids = (unit_id,)
            pair_ids: tuple[str, ...] = ()
            visited.add(unit_id)
        else:
            component = _connected_component(unit_id, adjacency)
            visited.update(component)
            component_ids = tuple(sorted(component))
            required_pairs: list[GmailTemporalEventIdentityPair] = []
            clique_safe = True
            for left_id, right_id in combinations(component_ids, 2):
                pair = pairs_by_units.get(frozenset((left_id, right_id)))
                if pair is None or consensus_by_pair[pair.pair_id] != "same_event":
                    clique_safe = False
                    if pair is not None:
                        required_pairs.append(pair)
                    continue
                required_pairs.append(pair)
            pair_ids = tuple(sorted({item.pair_id for item in required_pairs}))
            if not clique_safe:
                reviews.append(
                    GmailTemporalEventIdentityReview(
                        version="gmail_temporal_event_identity_review_v2",
                        review_id=_event_identity_review_id(component_ids, pair_ids),
                        reason="non_clique_or_contradictory_same_event_component",
                        unit_ids=component_ids,
                        pair_ids=pair_ids,
                    )
                )
                continue
        cluster_digest = _event_identity_cluster_digest(
            plan_fingerprint=plan.plan_fingerprint,
            unit_ids=component_ids,
            supporting_pair_ids=pair_ids,
            verdict_set_fingerprints=verdict_fingerprints,
        )
        anchor_unit_id = _expected_event_identity_anchor_unit_id(
            component_ids=component_ids,
            units_by_id=units,
            prior_resolution=prior_resolution,
        )
        event_identity_key = _event_identity_key(anchor_unit_id)
        cluster = GmailTemporalEventIdentityCluster(
            version="gmail_temporal_event_identity_cluster_v2",
            cluster_id=f"gteic_{cluster_digest}",
            event_identity_key=event_identity_key,
            event_identity_anchor_unit_id=anchor_unit_id,
            unit_ids=component_ids,
            supporting_pair_ids=pair_ids,
            provenance_ref=(
                gmail_temporal_source_bound_self_provenance(component_ids[0])
                if len(component_ids) == 1
                else f"gteiprov:{cluster_digest}"
            ),
        )
        clusters.append(cluster)
        for component_unit_id in component_ids:
            unit = units[component_unit_id]
            assertions.append(
                GmailTemporalEventIdentityAssertion(
                    version="gmail_temporal_event_identity_assertion_v2",
                    unit_id=component_unit_id,
                    projection_fingerprint=unit.projection_fingerprint,
                    artifact_id=unit.artifact_id,
                    hypothesis_id=unit.hypothesis_id,
                    event_identity_key=cluster.event_identity_key,
                    verification=(
                        "source_bound_self_identity"
                        if len(component_ids) == 1
                        else "external_verified"
                    ),
                    provenance_ref=cluster.provenance_ref,
                )
            )

    clusters_tuple = tuple(sorted(clusters, key=lambda item: item.cluster_id))
    reviews_tuple = tuple(sorted(reviews, key=lambda item: item.review_id))
    assertions_tuple = tuple(
        sorted(
            assertions,
            key=lambda item: (
                item.unit_id,
                item.projection_fingerprint,
                item.artifact_id,
                item.hypothesis_id,
            ),
        )
    )
    if prior_resolution is not None:
        current_key_by_unit = {
            unit_id: cluster.event_identity_key
            for cluster in clusters_tuple
            for unit_id in cluster.unit_ids
        }
        if any(
            {current_key_by_unit.get(unit_id) for unit_id in prior_cluster.unit_ids}
            != {prior_cluster.event_identity_key}
            for prior_cluster in prior_resolution.clusters
            if len(prior_cluster.unit_ids) > 1
        ):
            raise GmailTemporalEventIdentityError(
                "identity update contradicts a prior event cluster"
            )
    unit_ids = tuple(item.unit_id for item in plan.units)
    material = {
        "version": _RESOLUTION_VERSION,
        "plan_fingerprint": plan.plan_fingerprint,
        "prior_resolution_fingerprint": (
            prior_resolution.resolution_fingerprint
            if prior_resolution is not None
            else None
        ),
        "unit_ids": unit_ids,
        "verdict_set_fingerprints": verdict_fingerprints,
        "pair_consensus": [asdict(item) for item in pair_consensus],
        "clusters": [asdict(item) for item in clusters_tuple],
        "reviews": [asdict(item) for item in reviews_tuple],
        "assertions": [asdict(item) for item in assertions_tuple],
        "complete": True,
        "independent_invocations_verified": False,
        "candidate_authorization": False,
        "requires_defer": True,
        "routable": False,
    }
    result = GmailTemporalEventIdentityResolution(
        version="gmail_temporal_event_identity_resolution_v2",
        resolution_fingerprint=(
            "gteirx_" + hashlib.sha256(_canonical_bytes(material)).hexdigest()
        ),
        plan_fingerprint=plan.plan_fingerprint,
        prior_resolution_fingerprint=(
            prior_resolution.resolution_fingerprint
            if prior_resolution is not None
            else None
        ),
        unit_ids=unit_ids,
        verdict_set_fingerprints=verdict_fingerprints,
        pair_consensus=tuple(pair_consensus),
        clusters=clusters_tuple,
        reviews=reviews_tuple,
        assertions=assertions_tuple,
        complete=True,
    )
    _validate_resolution(result, plan=plan, prior_resolution=prior_resolution)
    return result


def validate_gmail_temporal_event_identity_resolution(
    *,
    plan: GmailTemporalEventIdentityPlan,
    resolution: GmailTemporalEventIdentityResolution,
    prior_resolution: GmailTemporalEventIdentityResolution | None = None,
) -> None:
    """Validate a resolution against its plan and exact parent authority."""

    _validate_plan(plan)
    _validate_resolution(
        resolution,
        plan=plan,
        prior_resolution=prior_resolution,
    )


def bind_gmail_temporal_event_identity_resolution(
    *,
    snapshot_authority: GmailTemporalThreadSnapshotAuthority,
    messages: tuple[GmailTemporalThreadMessageReview, ...],
    analysis_authorities: tuple[TemporalLeadAnalysis, ...],
    plan: GmailTemporalEventIdentityPlan,
    resolution: GmailTemporalEventIdentityResolution,
    prior_resolution: GmailTemporalEventIdentityResolution | None = None,
) -> tuple[GmailTemporalThreadMessageReview, ...]:
    """Overlay plan-validated assertions onto inputs without owner-lineage loss.

    Owner-verified assertions are not resolver output and this pure bridge has no
    trusted owner-receipt ledger from which to propagate or supersede them.  A
    bind therefore fails closed instead of silently replacing that stronger
    authority with source-self or external-model assertions.
    """

    try:
        _, normalized_messages = validate_gmail_temporal_thread_review_inputs(
            snapshot_authority=snapshot_authority,
            messages=messages,
        )
    except ValueError as exc:
        raise GmailTemporalEventIdentityError(
            "thread review authority is stale or invalid"
        ) from exc
    rebuilt_plan = plan_gmail_temporal_event_identity(
        snapshot_authority=snapshot_authority,
        messages=normalized_messages,
        analysis_authorities=analysis_authorities,
        pairs_per_page=plan.pairs_per_page,
    )
    if rebuilt_plan != plan:
        raise GmailTemporalEventIdentityError(
            "identity plan is not bound to the current thread review inputs"
        )
    validate_gmail_temporal_event_identity_resolution(
        plan=plan,
        resolution=resolution,
        prior_resolution=prior_resolution,
    )
    if any(
        assertion.verification == "owner_verified"
        for message in normalized_messages
        for assertion in message.identity_assertions
    ):
        raise GmailTemporalEventIdentityError(
            "owner-verified identity assertions require trusted owner-lineage "
            "propagation before resolver binding"
        )
    message_order_by_unit = {item.unit_id: item.message_order for item in plan.units}
    assertions_by_order: dict[int, list[GmailTemporalEventIdentityAssertion]] = {}
    for assertion in resolution.assertions:
        message_order = message_order_by_unit.get(assertion.unit_id)
        if message_order is None:
            raise GmailTemporalEventIdentityError(
                "identity assertion is not bound to a current plan unit"
            )
        assertions_by_order.setdefault(message_order, []).append(assertion)
    return tuple(
        replace(
            message,
            identity_assertions=tuple(
                sorted(
                    assertions_by_order.get(message_order, ()),
                    key=lambda item: (
                        item.unit_id,
                        item.projection_fingerprint,
                        item.artifact_id,
                        item.hypothesis_id,
                    ),
                )
            ),
        )
        for message_order, message in enumerate(normalized_messages, start=1)
    )


def _is_event_identity_hypothesis(
    *,
    artifact_id: str,
    hypothesis: GmailTemporalReviewHypothesis,
    subject_type_references: tuple[tuple[str, str], ...],
    structural_event_artifact_ids: set[str],
) -> bool:
    subject_types = {mention_type for _, mention_type in subject_type_references}
    if (
        not subject_types
        or not subject_types.issubset(_EVENT_CENTERED_SUBJECT_TYPES)
        or {mention_id for mention_id, _ in subject_type_references}
        != set(hypothesis.subject_mention_ids)
    ):
        return False
    if hypothesis.relation == "deadline":
        return False
    return (
        hypothesis.relation in {"occurrence", "scheduled_for"}
        or hypothesis.lifecycle
        in {
            "scheduled",
            "rescheduled_old",
            "rescheduled_replacement",
            "cancelled",
            "completed",
        }
        or artifact_id in structural_event_artifact_ids
    )


def _event_unit(
    *,
    message_order: int,
    message_authority_fingerprint: str,
    source_anchor: Mapping[str, str],
    projection_fingerprint: str,
    artifact: GmailTemporalReviewArtifact,
    hypothesis: GmailTemporalReviewHypothesis,
    subject_type_references: tuple[tuple[str, str], ...],
) -> GmailTemporalEventIdentityUnit:
    unit_id = gmail_temporal_event_identity_unit_id(
        source_anchor=source_anchor,
        artifact=artifact,
        hypothesis=hypothesis,
    )
    authority_material = {
        "version": _UNIT_AUTHORITY_VERSION,
        "unit_id": unit_id,
        "message_authority_fingerprint": message_authority_fingerprint,
        "projection_fingerprint": projection_fingerprint,
        "artifact": asdict(artifact),
        "hypothesis": asdict(hypothesis),
    }
    return GmailTemporalEventIdentityUnit(
        version="gmail_temporal_event_identity_unit_v2",
        unit_id=unit_id,
        message_order=message_order,
        message_authority_fingerprint=message_authority_fingerprint,
        unit_authority_fingerprint=(
            "gteiua_" + hashlib.sha256(_canonical_bytes(authority_material)).hexdigest()
        ),
        projection_fingerprint=projection_fingerprint,
        artifact_id=artifact.artifact_id,
        artifact_kind=artifact.kind,
        evidence_status=artifact.evidence_status,
        hypothesis_id=hypothesis.hypothesis_id,
        subject_mention_ids=hypothesis.subject_mention_ids,
        subject_type_references=subject_type_references,
        relation=hypothesis.relation,
        kind=hypothesis.kind,
        lifecycle=hypothesis.lifecycle,
        normalized_value=hypothesis.normalized_value,
        candidate_requires_defer=hypothesis.candidate_requires_defer,
    )


def _event_pair(
    left: GmailTemporalEventIdentityUnit,
    right: GmailTemporalEventIdentityUnit,
) -> GmailTemporalEventIdentityPair:
    left_id, right_id = sorted((left.unit_id, right.unit_id))
    return GmailTemporalEventIdentityPair(
        version="gmail_temporal_event_identity_pair_v2",
        pair_id=_event_pair_id(left_id, right_id),
        left_unit_id=left_id,
        right_unit_id=right_id,
    )


def _event_pair_id(left_unit_id: str, right_unit_id: str) -> str:
    left_id, right_id = sorted((left_unit_id, right_unit_id))
    material = {
        "version": _PAIR_VERSION,
        "left_unit_id": left_id,
        "right_unit_id": right_id,
    }
    return "gteipair_" + hashlib.sha256(_canonical_bytes(material)).hexdigest()


def _event_page(
    sequence: int,
    pairs: tuple[GmailTemporalEventIdentityPair, ...],
) -> GmailTemporalEventIdentityPage:
    pair_ids = tuple(item.pair_id for item in pairs)
    material = {
        "version": _PAGE_VERSION,
        "sequence": sequence,
        "pair_ids": pair_ids,
    }
    return GmailTemporalEventIdentityPage(
        version="gmail_temporal_event_identity_page_v2",
        sequence=sequence,
        page_fingerprint=(
            "gteipage_" + hashlib.sha256(_canonical_bytes(material)).hexdigest()
        ),
        pair_ids=pair_ids,
    )


def _validate_plan(plan: GmailTemporalEventIdentityPlan) -> None:
    if (
        not isinstance(plan, GmailTemporalEventIdentityPlan)
        or plan.version != _PLAN_VERSION
        or plan.complete is not True
        or plan.candidate_authorization is not False
        or plan.requires_defer is not True
        or plan.routable is not False
        or (
            plan.authorized_prior_resolution_fingerprint is not None
            and _OPAQUE.fullmatch(plan.authorized_prior_resolution_fingerprint) is None
        )
        or plan.max_units != MAX_EVENT_IDENTITY_UNITS
        or plan.max_pairs != MAX_EVENT_IDENTITY_PAIRS
        or isinstance(plan.pairs_per_page, bool)
        or not isinstance(plan.pairs_per_page, int)
        or not 1 <= plan.pairs_per_page <= MAX_PAIRS_PER_PAGE
        or len(plan.units) > plan.max_units
        or len(plan.pairs) > plan.max_pairs
    ):
        raise GmailTemporalEventIdentityError("identity plan structure is invalid")
    if _OPAQUE.fullmatch(plan.thread_authority_fingerprint) is None:
        raise GmailTemporalEventIdentityError("identity thread authority is invalid")
    if tuple(
        sorted(plan.units, key=lambda item: (item.message_order, item.unit_id))
    ) != (plan.units):
        raise GmailTemporalEventIdentityError("identity units are not canonical")
    if any(
        not isinstance(item, GmailTemporalEventIdentityUnit)
        or item.version != _UNIT_VERSION
        or item.message_order < 1
        or isinstance(item.message_order, bool)
        or _OPAQUE.fullmatch(item.unit_id) is None
        or _OPAQUE.fullmatch(item.message_authority_fingerprint) is None
        or _OPAQUE.fullmatch(item.unit_authority_fingerprint) is None
        or _OPAQUE.fullmatch(item.projection_fingerprint) is None
        or _OPAQUE.fullmatch(item.artifact_id) is None
        or _OPAQUE.fullmatch(item.hypothesis_id) is None
        or not isinstance(item.subject_mention_ids, tuple)
        or not item.subject_mention_ids
        or item.subject_mention_ids != tuple(sorted(item.subject_mention_ids))
        or not isinstance(item.subject_type_references, tuple)
        or not item.subject_type_references
        or item.subject_type_references != tuple(sorted(item.subject_type_references))
        or any(
            not isinstance(reference, tuple)
            or len(reference) != 2
            or reference[1] not in _EVENT_CENTERED_SUBJECT_TYPES
            for reference in item.subject_type_references
        )
        or tuple(reference[0] for reference in item.subject_type_references)
        != item.subject_mention_ids
        or item.candidate_authorization is not False
        or item.requires_defer is not True
        or item.routable is not False
        for item in plan.units
    ):
        raise GmailTemporalEventIdentityError("identity unit authority is invalid")
    unit_ids = {item.unit_id for item in plan.units}
    pair_ids = {item.pair_id for item in plan.pairs}
    if len(unit_ids) != len(plan.units) or len(pair_ids) != len(plan.pairs):
        raise GmailTemporalEventIdentityError("identity plan contains duplicate IDs")
    expected_pairs = tuple(
        sorted(
            (_event_pair(left, right) for left, right in combinations(plan.units, 2)),
            key=lambda item: item.pair_id,
        )
    )
    if plan.pairs != expected_pairs or any(
        not isinstance(item, GmailTemporalEventIdentityPair)
        or item.left_unit_id not in unit_ids
        or item.right_unit_id not in unit_ids
        or item.left_unit_id == item.right_unit_id
        or item.candidate_authorization is not False
        or item.requires_defer is not True
        or item.routable is not False
        for item in plan.pairs
    ):
        raise GmailTemporalEventIdentityError("identity plan pair authority is invalid")
    expected_pages = tuple(
        _event_page(sequence, plan.pairs[offset : offset + plan.pairs_per_page])
        for sequence, offset in enumerate(
            range(0, len(plan.pairs), plan.pairs_per_page),
            start=1,
        )
    )
    covered = tuple(pair_id for page in plan.pages for pair_id in page.pair_ids)
    if (
        plan.pages != expected_pages
        or len(covered) != len(set(covered))
        or set(covered) != pair_ids
        or any(
            page.sequence != index
            or not page.pair_ids
            or len(page.pair_ids) > plan.pairs_per_page
            for index, page in enumerate(plan.pages, start=1)
        )
        or (not plan.pairs and plan.pages)
    ):
        raise GmailTemporalEventIdentityError("identity plan page coverage is invalid")
    material = asdict(plan)
    material.pop("plan_fingerprint")
    expected = "gteip_" + hashlib.sha256(_canonical_bytes(material)).hexdigest()
    if plan.plan_fingerprint != expected:
        raise GmailTemporalEventIdentityError("identity plan fingerprint is stale")


def _validate_verdict_set(
    plan: GmailTemporalEventIdentityPlan,
    verdict_set: GmailTemporalEventIdentityVerdictSet,
) -> None:
    if (
        not isinstance(verdict_set, GmailTemporalEventIdentityVerdictSet)
        or verdict_set.version != _VERDICT_SET_VERSION
        or verdict_set.plan_fingerprint != plan.plan_fingerprint
        or verdict_set.run_ordinal not in {1, 2, 3}
        or isinstance(verdict_set.run_ordinal, bool)
        or _OPAQUE.fullmatch(verdict_set.invocation_id) is None
        or _SHA256_HEX.fullmatch(verdict_set.response_sha256) is None
        or verdict_set.independent_invocation_verified is not False
        or verdict_set.candidate_authorization is not False
        or verdict_set.requires_defer is not True
        or verdict_set.routable is not False
    ):
        raise GmailTemporalEventIdentityError(
            "identity verdict set is invalid or stale"
        )
    expected_pair_ids = tuple(item.pair_id for item in plan.pairs)
    actual_pair_ids = tuple(item.pair_id for item in verdict_set.verdicts)
    if actual_pair_ids != expected_pair_ids or any(
        item.verdict not in {"same_event", "different_event", "uncertain"}
        or item.routable is not False
        for item in verdict_set.verdicts
    ):
        raise GmailTemporalEventIdentityError("identity verdict coverage is incomplete")
    material = asdict(verdict_set)
    material.pop("verdict_set_fingerprint")
    expected = "gteivs_" + hashlib.sha256(_canonical_bytes(material)).hexdigest()
    if verdict_set.verdict_set_fingerprint != expected:
        raise GmailTemporalEventIdentityError("identity verdict fingerprint is stale")


def _validate_resolution(
    resolution: GmailTemporalEventIdentityResolution,
    *,
    plan: GmailTemporalEventIdentityPlan | None = None,
    prior_resolution: GmailTemporalEventIdentityResolution | None = None,
) -> None:
    if (
        not isinstance(resolution, GmailTemporalEventIdentityResolution)
        or resolution.version != _RESOLUTION_VERSION
        or not isinstance(resolution.unit_ids, tuple)
        or len(resolution.unit_ids) != len(set(resolution.unit_ids))
        or any(not _matches(_OPAQUE, item) for item in resolution.unit_ids)
        or not _matches(_OPAQUE, resolution.plan_fingerprint)
        or (
            resolution.prior_resolution_fingerprint is not None
            and not _matches(_OPAQUE, resolution.prior_resolution_fingerprint)
        )
        or not isinstance(resolution.verdict_set_fingerprints, tuple)
        or any(
            not _matches(_OPAQUE, item) for item in resolution.verdict_set_fingerprints
        )
        or not isinstance(resolution.pair_consensus, tuple)
        or not isinstance(resolution.clusters, tuple)
        or not isinstance(resolution.reviews, tuple)
        or not isinstance(resolution.assertions, tuple)
        or resolution.complete is not True
        or resolution.independent_invocations_verified is not False
        or resolution.candidate_authorization is not False
        or resolution.requires_defer is not True
        or resolution.routable is not False
    ):
        raise GmailTemporalEventIdentityError("identity resolution is invalid or stale")

    if prior_resolution is resolution:
        raise GmailTemporalEventIdentityError(
            "identity resolution cannot be its own prior authority"
        )
    if prior_resolution is not None:
        _validate_resolution(prior_resolution)
        if (
            resolution.prior_resolution_fingerprint
            != prior_resolution.resolution_fingerprint
            or not set(prior_resolution.unit_ids).issubset(resolution.unit_ids)
        ):
            raise GmailTemporalEventIdentityError(
                "identity resolution prior authority is missing or stale"
            )
    elif plan is not None and resolution.prior_resolution_fingerprint is not None:
        raise GmailTemporalEventIdentityError(
            "identity resolution requires its exact prior authority"
        )

    if plan is not None and (
        resolution.plan_fingerprint != plan.plan_fingerprint
        or resolution.unit_ids != tuple(item.unit_id for item in plan.units)
        or resolution.prior_resolution_fingerprint
        != plan.authorized_prior_resolution_fingerprint
    ):
        raise GmailTemporalEventIdentityError(
            "identity resolution does not match its complete plan authority"
        )

    unit_ids = set(resolution.unit_ids)
    expected_pair_units = dict(
        sorted(
            (
                _event_pair_id(left_id, right_id),
                tuple(sorted((left_id, right_id))),
            )
            for left_id, right_id in combinations(resolution.unit_ids, 2)
        )
    )
    expected_pair_ids = tuple(expected_pair_units)
    expected_verdict_set_count = 3 if expected_pair_ids else 0
    if (
        len(resolution.verdict_set_fingerprints) != expected_verdict_set_count
        or len(set(resolution.verdict_set_fingerprints)) != expected_verdict_set_count
    ):
        raise GmailTemporalEventIdentityError(
            "identity resolution verdict authority is invalid"
        )
    consensus_ids: set[str] = set()
    consensus_by_pair: dict[str, IdentityConsensus] = {}
    for item in resolution.pair_consensus:
        if (
            not isinstance(item, GmailTemporalEventIdentityPairConsensus)
            or item.version != _PAIR_CONSENSUS_VERSION
            or not _matches(_OPAQUE, item.pair_id)
            or item.pair_id in consensus_ids
            or item.consensus not in {"same_event", "different_event", "uncertain"}
            or not isinstance(item.run_verdicts, tuple)
            or len(item.run_verdicts) != 3
            or any(
                value not in {"same_event", "different_event", "uncertain"}
                for value in item.run_verdicts
            )
            or item.candidate_authorization is not False
            or item.requires_defer is not True
            or item.routable is not False
        ):
            raise GmailTemporalEventIdentityError(
                "identity resolution consensus is invalid"
            )
        expected_consensus = (
            item.run_verdicts[0] if len(set(item.run_verdicts)) == 1 else "uncertain"
        )
        if item.consensus != expected_consensus:
            raise GmailTemporalEventIdentityError(
                "identity resolution consensus is stale"
            )
        consensus_ids.add(item.pair_id)
        consensus_by_pair[item.pair_id] = item.consensus
    if tuple(item.pair_id for item in resolution.pair_consensus) != expected_pair_ids:
        raise GmailTemporalEventIdentityError(
            "identity resolution pair topology is invalid or incomplete"
        )

    expected_clusters, expected_reviews = _expected_resolution_components(
        unit_ids=resolution.unit_ids,
        pair_units=expected_pair_units,
        consensus_by_pair=consensus_by_pair,
    )
    plan_units_by_id = (
        {item.unit_id: item for item in plan.units} if plan is not None else {}
    )
    expected_anchors = (
        {
            component_ids: _expected_event_identity_anchor_unit_id(
                component_ids=component_ids,
                units_by_id=plan_units_by_id,
                prior_resolution=prior_resolution,
            )
            for component_ids in expected_clusters
        }
        if plan is not None
        else {}
    )
    if any(
        not isinstance(item, GmailTemporalEventIdentityCluster)
        for item in resolution.clusters
    ):
        raise GmailTemporalEventIdentityError("identity resolution cluster is invalid")
    if tuple(sorted(resolution.clusters, key=lambda item: item.cluster_id)) != (
        resolution.clusters
    ):
        raise GmailTemporalEventIdentityError(
            "identity resolution clusters are not canonical"
        )
    cluster_receipts: dict[tuple[str, str], tuple[int, str]] = {}
    event_keys: set[str] = set()
    for cluster in resolution.clusters:
        if (
            not isinstance(cluster, GmailTemporalEventIdentityCluster)
            or cluster.version != _CLUSTER_VERSION
            or not _matches(_OPAQUE, cluster.cluster_id)
            or not _matches(_OPAQUE, cluster.event_identity_key)
            or not _matches(_OPAQUE, cluster.event_identity_anchor_unit_id)
            or not _matches(_OPAQUE, cluster.provenance_ref)
            or not isinstance(cluster.unit_ids, tuple)
            or not cluster.unit_ids
            or len(cluster.unit_ids) != len(set(cluster.unit_ids))
            or not set(cluster.unit_ids).issubset(unit_ids)
            or cluster.event_identity_anchor_unit_id not in cluster.unit_ids
            or cluster.unit_ids != tuple(sorted(cluster.unit_ids))
            or cluster.event_identity_key in event_keys
            or not isinstance(cluster.supporting_pair_ids, tuple)
            or cluster.candidate_authorization is not False
            or cluster.requires_defer is not True
            or cluster.routable is not False
        ):
            raise GmailTemporalEventIdentityError(
                "identity resolution cluster is invalid"
            )
        expected_supporting_pairs = expected_clusters.get(cluster.unit_ids)
        if expected_supporting_pairs is None:
            raise GmailTemporalEventIdentityError(
                "identity resolution cluster topology is not implied by consensus"
            )
        cluster_digest = _event_identity_cluster_digest(
            plan_fingerprint=resolution.plan_fingerprint,
            unit_ids=cluster.unit_ids,
            supporting_pair_ids=expected_supporting_pairs,
            verdict_set_fingerprints=resolution.verdict_set_fingerprints,
        )
        expected_provenance = (
            gmail_temporal_source_bound_self_provenance(cluster.unit_ids[0])
            if len(cluster.unit_ids) == 1
            else f"gteiprov:{cluster_digest}"
        )
        if (
            cluster.supporting_pair_ids != expected_supporting_pairs
            or cluster.cluster_id != f"gteic_{cluster_digest}"
            or cluster.provenance_ref != expected_provenance
        ):
            raise GmailTemporalEventIdentityError(
                "identity resolution cluster receipt or topology is stale"
            )
        if cluster.event_identity_key != _event_identity_key(
            cluster.event_identity_anchor_unit_id
        ):
            raise GmailTemporalEventIdentityError(
                "identity resolution event key does not match its exact anchor"
            )
        expected_anchor = expected_anchors.get(cluster.unit_ids)
        if (
            plan is not None
            and cluster.event_identity_anchor_unit_id != expected_anchor
        ):
            raise GmailTemporalEventIdentityError(
                "identity resolution event anchor is not canonical or prior-derived"
            )
        event_keys.add(cluster.event_identity_key)
        cluster_receipts[(cluster.event_identity_key, cluster.provenance_ref)] = (
            len(cluster.unit_ids),
            (
                "source_bound_self_identity"
                if len(cluster.unit_ids) == 1
                else "external_verified"
            ),
        )
    if {cluster.unit_ids for cluster in resolution.clusters} != set(expected_clusters):
        raise GmailTemporalEventIdentityError(
            "identity resolution cluster coverage is incomplete"
        )

    if any(
        not isinstance(item, GmailTemporalEventIdentityReview)
        for item in resolution.reviews
    ):
        raise GmailTemporalEventIdentityError("identity resolution review is invalid")
    if tuple(sorted(resolution.reviews, key=lambda item: item.review_id)) != (
        resolution.reviews
    ):
        raise GmailTemporalEventIdentityError(
            "identity resolution reviews are not canonical"
        )
    for review in resolution.reviews:
        if (
            not isinstance(review, GmailTemporalEventIdentityReview)
            or review.version != _REVIEW_VERSION
            or review.reason != "non_clique_or_contradictory_same_event_component"
            or not _matches(_OPAQUE, review.review_id)
            or not isinstance(review.unit_ids, tuple)
            or len(review.unit_ids) < 2
            or len(review.unit_ids) != len(set(review.unit_ids))
            or not set(review.unit_ids).issubset(unit_ids)
            or review.unit_ids != tuple(sorted(review.unit_ids))
            or not isinstance(review.pair_ids, tuple)
            or review.candidate_authorization is not False
            or review.requires_defer is not True
            or review.routable is not False
        ):
            raise GmailTemporalEventIdentityError(
                "identity resolution review is invalid"
            )
        expected_review_pairs = expected_reviews.get(review.unit_ids)
        if (
            expected_review_pairs is None
            or review.pair_ids != expected_review_pairs
            or review.review_id
            != _event_identity_review_id(review.unit_ids, expected_review_pairs)
        ):
            raise GmailTemporalEventIdentityError(
                "identity resolution review receipt or topology is stale"
            )
    if {review.unit_ids for review in resolution.reviews} != set(expected_reviews):
        raise GmailTemporalEventIdentityError(
            "identity resolution review coverage is incomplete"
        )

    assertion_signatures: set[tuple[str, ...]] = set()
    assertion_counts: dict[tuple[str, str], int] = {}
    for assertion in resolution.assertions:
        if not isinstance(assertion, GmailTemporalEventIdentityAssertion):
            raise GmailTemporalEventIdentityError(
                "identity resolution assertion is invalid"
            )
        signature = (
            assertion.unit_id,
            assertion.projection_fingerprint,
            assertion.artifact_id,
            assertion.hypothesis_id,
            assertion.event_identity_key,
            assertion.provenance_ref,
        )
        receipt = (assertion.event_identity_key, assertion.provenance_ref)
        expected_receipt = cluster_receipts.get(receipt)
        if (
            assertion.version != "gmail_temporal_event_identity_assertion_v2"
            or expected_receipt is None
            or assertion.verification != expected_receipt[1]
            or not _matches(_OPAQUE, assertion.unit_id)
            or not _matches(_OPAQUE, assertion.projection_fingerprint)
            or not _matches(_OPAQUE, assertion.artifact_id)
            or not _matches(_OPAQUE, assertion.hypothesis_id)
            or signature in assertion_signatures
            or assertion.candidate_authorization is not False
            or assertion.requires_defer is not True
            or assertion.routable is not False
        ):
            raise GmailTemporalEventIdentityError(
                "identity resolution assertion is invalid"
            )
        assertion_signatures.add(signature)
        assertion_counts[receipt] = assertion_counts.get(receipt, 0) + 1
    expected_assertion_counts = {
        receipt: expected[0] for receipt, expected in cluster_receipts.items()
    }
    if assertion_counts != expected_assertion_counts:
        raise GmailTemporalEventIdentityError(
            "identity resolution assertion coverage is incomplete"
        )
    if (
        tuple(
            sorted(
                resolution.assertions,
                key=lambda item: (
                    item.unit_id,
                    item.projection_fingerprint,
                    item.artifact_id,
                    item.hypothesis_id,
                ),
            )
        )
        != resolution.assertions
    ):
        raise GmailTemporalEventIdentityError(
            "identity resolution assertions are not canonical"
        )

    if plan is not None:
        units_by_id = {item.unit_id: item for item in plan.units}
        expected_assertions = tuple(
            sorted(
                (
                    GmailTemporalEventIdentityAssertion(
                        version="gmail_temporal_event_identity_assertion_v2",
                        unit_id=unit_id,
                        projection_fingerprint=units_by_id[
                            unit_id
                        ].projection_fingerprint,
                        artifact_id=units_by_id[unit_id].artifact_id,
                        hypothesis_id=units_by_id[unit_id].hypothesis_id,
                        event_identity_key=cluster.event_identity_key,
                        verification=(
                            "source_bound_self_identity"
                            if len(cluster.unit_ids) == 1
                            else "external_verified"
                        ),
                        provenance_ref=cluster.provenance_ref,
                    )
                    for cluster in resolution.clusters
                    for unit_id in cluster.unit_ids
                ),
                key=lambda item: (
                    item.unit_id,
                    item.projection_fingerprint,
                    item.artifact_id,
                    item.hypothesis_id,
                ),
            )
        )
        if expected_assertions != resolution.assertions:
            raise GmailTemporalEventIdentityError(
                "identity assertions do not match their exact plan units"
            )

    material = asdict(resolution)
    material.pop("resolution_fingerprint")
    expected = "gteirx_" + hashlib.sha256(_canonical_bytes(material)).hexdigest()
    if resolution.resolution_fingerprint != expected:
        raise GmailTemporalEventIdentityError(
            "identity resolution fingerprint is stale"
        )


def _connected_component(start: str, adjacency: Mapping[str, set[str]]) -> set[str]:
    pending = [start]
    found: set[str] = set()
    while pending:
        current = pending.pop()
        if current in found:
            continue
        found.add(current)
        pending.extend(sorted(adjacency[current] - found, reverse=True))
    return found


def _expected_resolution_components(
    *,
    unit_ids: tuple[str, ...],
    pair_units: Mapping[str, tuple[str, ...]],
    consensus_by_pair: Mapping[str, IdentityConsensus],
) -> tuple[
    dict[tuple[str, ...], tuple[str, ...]],
    dict[tuple[str, ...], tuple[str, ...]],
]:
    adjacency: dict[str, set[str]] = {unit_id: set() for unit_id in unit_ids}
    for pair_id, endpoints in pair_units.items():
        if consensus_by_pair[pair_id] != "same_event":
            continue
        left_id, right_id = endpoints
        adjacency[left_id].add(right_id)
        adjacency[right_id].add(left_id)

    clusters: dict[tuple[str, ...], tuple[str, ...]] = {}
    reviews: dict[tuple[str, ...], tuple[str, ...]] = {}
    visited: set[str] = set()
    for unit_id in sorted(unit_ids):
        if unit_id in visited:
            continue
        if not adjacency[unit_id]:
            clusters[(unit_id,)] = ()
            visited.add(unit_id)
            continue
        component_ids = tuple(sorted(_connected_component(unit_id, adjacency)))
        visited.update(component_ids)
        pair_ids = tuple(
            sorted(
                _event_pair_id(left_id, right_id)
                for left_id, right_id in combinations(component_ids, 2)
            )
        )
        if all(consensus_by_pair[pair_id] == "same_event" for pair_id in pair_ids):
            clusters[component_ids] = pair_ids
        else:
            reviews[component_ids] = pair_ids
    return clusters, reviews


def _event_identity_key(anchor_unit_id: str) -> str:
    return gmail_temporal_source_bound_event_identity_key(anchor_unit_id)


def _event_identity_cluster_digest(
    *,
    plan_fingerprint: str,
    unit_ids: tuple[str, ...],
    supporting_pair_ids: tuple[str, ...],
    verdict_set_fingerprints: tuple[str, ...],
) -> str:
    material = {
        "version": _CLUSTER_VERSION,
        "plan_fingerprint": plan_fingerprint,
        "unit_ids": unit_ids,
        "supporting_pair_ids": supporting_pair_ids,
        "verdict_set_fingerprints": verdict_set_fingerprints,
    }
    return hashlib.sha256(_canonical_bytes(material)).hexdigest()


def _event_identity_review_id(
    unit_ids: tuple[str, ...],
    pair_ids: tuple[str, ...],
) -> str:
    material = {
        "version": _REVIEW_VERSION,
        "reason": "non_clique_or_contradictory_same_event_component",
        "unit_ids": unit_ids,
        "pair_ids": pair_ids,
    }
    return "gteir_" + hashlib.sha256(_canonical_bytes(material)).hexdigest()


def _expected_event_identity_anchor_unit_id(
    *,
    component_ids: tuple[str, ...],
    units_by_id: Mapping[str, GmailTemporalEventIdentityUnit],
    prior_resolution: GmailTemporalEventIdentityResolution | None,
) -> str:
    """Choose the sole key anchor authorized by current order and exact parent."""

    component = set(component_ids)
    prior_clusters = prior_resolution.clusters if prior_resolution is not None else ()
    prior_multi_clusters = tuple(
        cluster
        for cluster in prior_clusters
        if len(cluster.unit_ids) > 1 and component.intersection(cluster.unit_ids)
    )
    if len(prior_multi_clusters) > 1:
        raise GmailTemporalEventIdentityError(
            "identity update would merge multiple prior event keys"
        )
    if prior_multi_clusters:
        prior_cluster = prior_multi_clusters[0]
        if not set(prior_cluster.unit_ids).issubset(component):
            raise GmailTemporalEventIdentityError(
                "identity update contradicts a prior event cluster"
            )
        return prior_cluster.event_identity_anchor_unit_id

    prior_singleton_anchors = tuple(
        cluster.event_identity_anchor_unit_id
        for cluster in prior_clusters
        if len(cluster.unit_ids) == 1 and cluster.unit_ids[0] in component
    )
    candidates = prior_singleton_anchors or component_ids
    return min(
        candidates,
        key=lambda unit_id: (units_by_id[unit_id].message_order, unit_id),
    )


def _verdict(value: Any) -> IdentityVerdict:
    if value not in {"same_event", "different_event", "uncertain"}:
        raise GmailTemporalEventIdentityError("identity verdict is invalid")
    return value


def _opaque(value: Any, name: str) -> str:
    if not isinstance(value, str) or _OPAQUE.fullmatch(value) is None:
        raise GmailTemporalEventIdentityError(f"{name} is invalid")
    return value


def _sha256(value: Any, name: str) -> str:
    if not isinstance(value, str) or _SHA256_HEX.fullmatch(value) is None:
        raise GmailTemporalEventIdentityError(f"{name} is invalid")
    return value


def _matches(pattern: re.Pattern[str], value: Any) -> bool:
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
