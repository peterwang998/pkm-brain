from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from typing import Any, Literal, Mapping

from .gmail_temporal_batching import (
    GmailTemporalBatchPlan,
    GmailTemporalSelectorBatch,
    plan_gmail_temporal_selector_batches,
)
from .gmail_temporal_frontier import (
    GmailTemporalCandidateEnsembleVerdictSet,
    GmailTemporalCandidatePagePlan,
    GmailTemporalVerificationCandidate,
    build_gmail_temporal_candidate_frontier,
    gmail_temporal_candidate_ensemble_policy_fingerprint,
    plan_gmail_temporal_candidate_pages,
    validate_gmail_temporal_candidate_verdict_set,
)
from .gmail_temporal_leads import TemporalExpression, TemporalLeadAnalysis
from .gmail_temporal_selection import classify_gmail_temporal_subject_pair


_PROJECTION_VERSION = "gmail_temporal_review_projection_v1"
_ARTIFACT_VERSION = "gmail_temporal_review_artifact_v1"
_HYPOTHESIS_VERSION = "gmail_temporal_review_hypothesis_v1"
_CLUSTER_REVIEW_VERSION = "gmail_temporal_review_cluster_review_v1"
_GROUP_VERSION = "gmail_temporal_review_group_v1"
_GROUP_MEMBER_VERSION = "gmail_temporal_review_group_member_v1"
_GROUPING_POLICY_VERSION = "gmail_temporal_review_grouping_policy_v1"

ReviewArtifactKind = Literal["supported_citation", "uncertainty_sidecar"]
ReviewEvidenceStatus = Literal["supported", "uncertain"]
ReviewGroupKind = Literal[
    "single",
    "alternatives",
    "reschedule",
    "split_semantics",
]
ReviewGroupRole = Literal[
    "independent",
    "alternative",
    "rescheduled_old",
    "rescheduled_replacement",
    "unresolved",
]
ReviewGroupCoverage = Literal["complete", "incomplete", "conflicted"]
ReviewGroupMemberState = Literal["present", "missing", "conflicted"]

_ALTERNATIVE_PREFIX_RE = re.compile(
    r"(?:"
    r"\b(?:alternative|available|possible)\s+(?:dates?|times?|options?)\s*"
    r"(?:are|include|:)?|"
    r"\b(?:dates?|times?|options?)\s+(?:are|could\s+be|include)|"
    r"\b(?:either|one\s+of)|"
    r"\b(?:is|are|on|for|could\s+be|may\s+be|might\s+be)"
    r")\s*$",
    re.IGNORECASE,
)
_ALTERNATIVE_COMMA_RE = re.compile(r"\s*,\s*")
_ALTERNATIVE_OR_RE = re.compile(r"\s*(?:,\s*)?\bor\b\s*", re.IGNORECASE)
_RESCHEDULE_PREFIX_RE = re.compile(
    r"\b(?:changed|moved|rescheduled)\b[^.!?;\r\n]{0,90}\bfrom\s*$",
    re.IGNORECASE,
)
_RESCHEDULE_CONNECTOR_RE = re.compile(r"\s+to\s+", re.IGNORECASE)


class GmailTemporalReviewError(ValueError):
    """Raised when a review projection exceeds its immutable authority."""


@dataclass(frozen=True)
class GmailTemporalReviewBatchResult:
    """One validated three-run result paired with its exact batch and page plan."""

    batch: GmailTemporalSelectorBatch
    page_plan: GmailTemporalCandidatePagePlan
    ensemble: GmailTemporalCandidateEnsembleVerdictSet
    component_evidence_fingerprints: tuple[str, ...] = ()


@dataclass(frozen=True)
class GmailTemporalReviewHypothesis:
    """One semantic hypothesis after reducer-equivalent subject aliases collapse."""

    version: Literal["gmail_temporal_review_hypothesis_v1"]
    hypothesis_id: str
    expression_id: str
    subject_mention_ids: tuple[str, ...]
    lifecycle_mention_ids: tuple[str, ...]
    relation: str
    kind: str
    lifecycle: str
    normalized_value: str | None
    candidate_ids: tuple[str, ...]
    candidate_requires_defer: bool
    requires_defer: Literal[True] = True
    routable: Literal[False] = False


@dataclass(frozen=True)
class GmailTemporalReviewArtifact:
    """A supported citation or uncertainty sidecar visible to a review consumer."""

    version: Literal["gmail_temporal_review_artifact_v1"]
    artifact_id: str
    kind: ReviewArtifactKind
    evidence_status: ReviewEvidenceStatus
    batch_fingerprint: str
    frontier_fingerprint: str
    parent_cluster_id: str
    candidate_ids: tuple[str, ...]
    hypotheses: tuple[GmailTemporalReviewHypothesis, ...]
    candidate_authorization: Literal[True] = True
    requires_defer: Literal[True] = True
    routable: Literal[False] = False


@dataclass(frozen=True)
class GmailTemporalReviewClusterReview:
    """Split semantic triage that deliberately authorizes no candidate."""

    version: Literal["gmail_temporal_review_cluster_review_v1"]
    review_id: str
    batch_fingerprint: str
    frontier_fingerprint: str
    cluster_id: str
    expression_id: str
    candidate_ids: tuple[str, ...]
    reason: Literal["split_semantics_unresolved"]
    candidate_authorization: Literal[False] = False
    requires_defer: Literal[True] = True
    routable: Literal[False] = False


@dataclass(frozen=True)
class GmailTemporalReviewGroupMember:
    """One ordered structural role and its review artifact, if recovered."""

    version: Literal["gmail_temporal_review_group_member_v1"]
    member_id: str
    expression_id: str
    role: ReviewGroupRole
    source_order: int | None
    state: ReviewGroupMemberState
    artifact_ids: tuple[str, ...]
    cluster_review_ids: tuple[str, ...]
    subject_family_ids: tuple[str, ...]
    reasons: tuple[str, ...]
    candidate_authorization: Literal[False] = False
    requires_defer: Literal[True] = True
    routable: Literal[False] = False


@dataclass(frozen=True)
class GmailTemporalReviewGroup:
    """Message-level temporal structure; metadata, never another artifact."""

    version: Literal["gmail_temporal_review_group_v1"]
    group_id: str
    kind: ReviewGroupKind
    coverage: ReviewGroupCoverage
    source_start: int
    source_end: int
    subject_family_id: str | None
    members: tuple[GmailTemporalReviewGroupMember, ...]
    reasons: tuple[str, ...]
    candidate_authorization: Literal[False] = False
    requires_defer: Literal[True] = True
    routable: Literal[False] = False


@dataclass(frozen=True)
class GmailTemporalReviewProjection:
    """Canonical, raw-source-content-free output for one complete Gmail message."""

    version: Literal["gmail_temporal_review_projection_v1"]
    projection_fingerprint: str
    analysis_fingerprint: str
    source_sha256: str
    batch_plan_fingerprint: str
    ensemble_policy_fingerprint: str
    grouping_policy_fingerprint: str
    independent_invocations_verified: Literal[False]
    component_evidence_fingerprints: tuple[str, ...]
    artifacts: tuple[GmailTemporalReviewArtifact, ...]
    cluster_reviews: tuple[GmailTemporalReviewClusterReview, ...]
    groups: tuple[GmailTemporalReviewGroup, ...]
    complete: Literal[True]
    requires_defer: Literal[True] = True
    routable: Literal[False] = False


@dataclass(frozen=True)
class _StructuralMember:
    expression_id: str
    role: ReviewGroupRole
    source_order: int


@dataclass(frozen=True)
class _StructuralFrame:
    frame_id: str
    kind: Literal["alternatives", "reschedule"]
    source_start: int
    source_end: int
    members: tuple[_StructuralMember, ...]


@dataclass(frozen=True)
class _BatchAuthority:
    batch: GmailTemporalSelectorBatch
    page_plan: GmailTemporalCandidatePagePlan
    ensemble: GmailTemporalCandidateEnsembleVerdictSet
    candidates: tuple[GmailTemporalVerificationCandidate, ...]
    cluster_candidates: Mapping[str, tuple[str, ...]]
    cluster_expression: Mapping[str, str]
    candidate_cluster: Mapping[str, str]


def gmail_temporal_review_grouping_policy_fingerprint() -> str:
    """Bind exact structural grammar and fail-closed projection behavior."""

    material = {
        "version": _GROUPING_POLICY_VERSION,
        "artifact_hypothesis_signature": [
            "expression_id",
            "relation",
            "kind",
            "lifecycle",
            "normalized_value",
        ],
        "candidate_defer_state": "conservative_any_alias_requires_defer",
        "alias_policy": "existing_source_verified_subject_alias_components",
        "alternative_grammar": {
            "same_field_and_segment": True,
            "ordered_expression_list": True,
            "requires_final_or": True,
            "prefix_pattern": _ALTERNATIVE_PREFIX_RE.pattern,
            "comma_pattern": _ALTERNATIVE_COMMA_RE.pattern,
            "or_pattern": _ALTERNATIVE_OR_RE.pattern,
        },
        "reschedule_grammar": {
            "same_field_and_segment": True,
            "requires_one_rescheduled_lifecycle_mention": True,
            "prefix_pattern": _RESCHEDULE_PREFIX_RE.pattern,
            "connector_pattern": _RESCHEDULE_CONNECTOR_RE.pattern,
            "roles_follow_source_order_not_value_order": True,
        },
        "groups_are_artifacts": False,
        "incomplete_or_conflicted_authorizes_candidates": False,
        "cluster_reviews_authorize_candidates": False,
        "all_outputs_require_defer": True,
        "all_outputs_routable": False,
    }
    return "gtrgp_" + hashlib.sha256(_canonical_bytes(material)).hexdigest()


def project_gmail_temporal_review(
    *,
    text: str,
    analysis: TemporalLeadAnalysis,
    batch_plan: GmailTemporalBatchPlan,
    batch_results: tuple[GmailTemporalReviewBatchResult, ...],
) -> GmailTemporalReviewProjection:
    """Project one complete message into artifacts plus non-authorizing groups.

    Structural grouping is detected from the immutable source before verdicts are
    considered, so a missed endpoint remains an explicit incomplete member. The
    existing candidate relation, kind, lifecycle, and normalization are copied
    unchanged into hypotheses. In particular, source-order reschedule roles do
    not rewrite an unresolved candidate's ``unknown`` lifecycle.
    """

    if not isinstance(text, str):
        raise TypeError("text must be a string")
    if not isinstance(analysis, TemporalLeadAnalysis):
        raise GmailTemporalReviewError("temporal analysis is invalid")
    if not isinstance(batch_plan, GmailTemporalBatchPlan):
        raise GmailTemporalReviewError("temporal batch plan is invalid")
    expected_plan = plan_gmail_temporal_selector_batches(
        text=text,
        analysis=analysis,
        caps=batch_plan.caps,
    )
    if batch_plan != expected_plan:
        raise GmailTemporalReviewError("temporal batch plan is stale or mutated")
    if batch_plan.omissions:
        raise GmailTemporalReviewError(
            "review projection requires a complete batch plan"
        )
    if not isinstance(batch_results, tuple) or any(
        not isinstance(item, GmailTemporalReviewBatchResult) for item in batch_results
    ):
        raise GmailTemporalReviewError("review batch results are invalid")

    by_batch_fingerprint: dict[str, GmailTemporalReviewBatchResult] = {}
    for item in batch_results:
        fingerprint = item.batch.manifest.batch_fingerprint
        if fingerprint in by_batch_fingerprint:
            raise GmailTemporalReviewError("review batch result is duplicated")
        by_batch_fingerprint[fingerprint] = item
    expected_fingerprints = tuple(
        batch.manifest.batch_fingerprint for batch in batch_plan.batches
    )
    if set(by_batch_fingerprint) != set(expected_fingerprints):
        raise GmailTemporalReviewError(
            "review batch results do not cover the batch plan exactly"
        )

    authorities: list[_BatchAuthority] = []
    component_evidence_fingerprints: tuple[str, ...] | None = None
    for expected_batch in batch_plan.batches:
        item = by_batch_fingerprint[expected_batch.manifest.batch_fingerprint]
        if (
            not isinstance(item.component_evidence_fingerprints, tuple)
            or len(item.component_evidence_fingerprints) not in {0, 3}
            or any(
                not isinstance(value, str) or not value
                for value in item.component_evidence_fingerprints
            )
        ):
            raise GmailTemporalReviewError(
                "component evidence fingerprints are malformed"
            )
        if component_evidence_fingerprints is None:
            component_evidence_fingerprints = item.component_evidence_fingerprints
        elif component_evidence_fingerprints != item.component_evidence_fingerprints:
            raise GmailTemporalReviewError(
                "review batches disagree on component evidence fingerprints"
            )
        authorities.append(
            _validate_batch_result(
                analysis=analysis,
                expected_batch=expected_batch,
                result=item,
            )
        )
    component_evidence_fingerprints = component_evidence_fingerprints or ()

    candidate_index: dict[str, GmailTemporalVerificationCandidate] = {}
    candidate_batch: dict[str, _BatchAuthority] = {}
    candidate_cluster: dict[str, str] = {}
    for authority in authorities:
        for candidate in authority.candidates:
            if candidate.candidate_id in candidate_index:
                raise GmailTemporalReviewError("candidate authority is duplicated")
            candidate_index[candidate.candidate_id] = candidate
            candidate_batch[candidate.candidate_id] = authority
            candidate_cluster[candidate.candidate_id] = authority.candidate_cluster[
                candidate.candidate_id
            ]

    artifacts: list[GmailTemporalReviewArtifact] = []
    cluster_reviews: list[GmailTemporalReviewClusterReview] = []
    for authority in authorities:
        artifacts.extend(_artifacts_for_authority(authority))
        cluster_reviews.extend(_cluster_reviews_for_authority(authority))
    artifacts_tuple = tuple(
        sorted(
            artifacts,
            key=lambda item: (
                _expression_rank(analysis, item.hypotheses[0].expression_id),
                item.artifact_id,
            ),
        )
    )
    cluster_reviews_tuple = tuple(
        sorted(
            cluster_reviews,
            key=lambda item: (
                _expression_rank(analysis, item.expression_id),
                item.review_id,
            ),
        )
    )

    subject_families = _subject_alias_families(
        analysis=analysis,
        batches=batch_plan.batches,
        candidates=tuple(candidate_index.values()),
    )
    frames = _structural_frames(text=text, analysis=analysis)
    groups = _review_groups(
        analysis=analysis,
        frames=frames,
        artifacts=artifacts_tuple,
        cluster_reviews=cluster_reviews_tuple,
        candidates=candidate_index,
        subject_families=subject_families,
    )

    grouping_policy_fingerprint = gmail_temporal_review_grouping_policy_fingerprint()
    material = {
        "version": _PROJECTION_VERSION,
        "analysis_fingerprint": analysis.snapshot_fingerprint,
        "source_sha256": batch_plan.source_sha256,
        "batch_plan_fingerprint": batch_plan.plan_fingerprint,
        "ensemble_policy_fingerprint": (
            gmail_temporal_candidate_ensemble_policy_fingerprint()
        ),
        "grouping_policy_fingerprint": grouping_policy_fingerprint,
        "independent_invocations_verified": False,
        "component_evidence_fingerprints": list(component_evidence_fingerprints),
        "artifacts": [_jsonable(asdict(item)) for item in artifacts_tuple],
        "cluster_reviews": [_jsonable(asdict(item)) for item in cluster_reviews_tuple],
        "groups": [_jsonable(asdict(item)) for item in groups],
        "complete": True,
        "requires_defer": True,
        "routable": False,
    }
    projection = GmailTemporalReviewProjection(
        version="gmail_temporal_review_projection_v1",
        projection_fingerprint="gtrp_"
        + hashlib.sha256(_canonical_bytes(material)).hexdigest(),
        analysis_fingerprint=analysis.snapshot_fingerprint,
        source_sha256=batch_plan.source_sha256,
        batch_plan_fingerprint=batch_plan.plan_fingerprint,
        ensemble_policy_fingerprint=(
            gmail_temporal_candidate_ensemble_policy_fingerprint()
        ),
        grouping_policy_fingerprint=grouping_policy_fingerprint,
        independent_invocations_verified=False,
        component_evidence_fingerprints=component_evidence_fingerprints,
        artifacts=artifacts_tuple,
        cluster_reviews=cluster_reviews_tuple,
        groups=groups,
        complete=True,
    )
    _validate_projection(projection)
    return projection


def gmail_temporal_review_projection_payload(
    projection: GmailTemporalReviewProjection,
) -> dict[str, object]:
    """Return the validated canonical JSON-safe projection payload."""

    _validate_projection(projection)
    value = _jsonable(asdict(projection))
    if not isinstance(value, dict):
        raise GmailTemporalReviewError("review projection payload is malformed")
    return value


def canonical_gmail_temporal_review_projection_bytes(
    projection: GmailTemporalReviewProjection,
) -> bytes:
    """Return canonical compact UTF-8 bytes suitable for hash-bound storage."""

    return _canonical_bytes(gmail_temporal_review_projection_payload(projection))


def _validate_batch_result(
    *,
    analysis: TemporalLeadAnalysis,
    expected_batch: GmailTemporalSelectorBatch,
    result: GmailTemporalReviewBatchResult,
) -> _BatchAuthority:
    if result.batch != expected_batch:
        raise GmailTemporalReviewError("review result batch is stale or mutated")
    page_plan = result.page_plan
    if not isinstance(page_plan, GmailTemporalCandidatePagePlan):
        raise GmailTemporalReviewError("candidate page plan is invalid")
    expected_page_plan = plan_gmail_temporal_candidate_pages(
        analysis=analysis,
        batch=expected_batch,
        max_clusters_per_page=page_plan.max_clusters_per_page,
        max_candidates_per_page=page_plan.max_candidates_per_page,
        max_payload_bytes=page_plan.max_payload_bytes,
    )
    if page_plan != expected_page_plan or not page_plan.complete:
        raise GmailTemporalReviewError("candidate page plan is stale or incomplete")
    ensemble = result.ensemble
    if (
        not isinstance(ensemble, GmailTemporalCandidateEnsembleVerdictSet)
        or ensemble.version != "gmail_temporal_candidate_three_run_ensemble_v3"
        or ensemble.policy_version != "gmail_temporal_candidate_three_run_consensus_v3"
        or ensemble.policy_fingerprint
        != gmail_temporal_candidate_ensemble_policy_fingerprint()
        or ensemble.plan_fingerprint != page_plan.plan_fingerprint
        or ensemble.frontier_fingerprint != page_plan.frontier_fingerprint
        or ensemble.run_count != 3
        or ensemble.routable is not False
    ):
        raise GmailTemporalReviewError("candidate ensemble authority is invalid")
    expected_verdict_set = validate_gmail_temporal_candidate_verdict_set(
        analysis=analysis,
        batch=expected_batch,
        plan=page_plan,
        rows=ensemble.consensus_rows,
    )
    if ensemble.verdict_set != expected_verdict_set:
        raise GmailTemporalReviewError("candidate ensemble verdict set is stale")

    frontier = build_gmail_temporal_candidate_frontier(
        analysis=analysis,
        batch=expected_batch,
    )
    cluster_candidates: dict[str, list[str]] = {}
    cluster_expression: dict[str, str] = {}
    candidate_cluster: dict[str, str] = {}
    for page in page_plan.pages:
        for cluster in page.clusters:
            previous_expression = cluster_expression.setdefault(
                cluster.cluster_id,
                cluster.expression_id,
            )
            if previous_expression != cluster.expression_id:
                raise GmailTemporalReviewError("parent cluster expression is unstable")
            values = cluster_candidates.setdefault(cluster.cluster_id, [])
            for candidate_id in cluster.candidate_ids:
                if candidate_id in candidate_cluster:
                    raise GmailTemporalReviewError("candidate cluster is duplicated")
                candidate_cluster[candidate_id] = cluster.cluster_id
                values.append(candidate_id)
    if set(candidate_cluster) != {item.candidate_id for item in frontier.candidates}:
        raise GmailTemporalReviewError("candidate clusters do not cover the frontier")

    review_cluster_ids: set[str] = set()
    accepted_ids = set(ensemble.verdict_set.supported_candidate_ids) | {
        candidate_id
        for uncertainty in ensemble.verdict_set.uncertain_clusters
        for candidate_id in uncertainty.plausible_candidate_ids
    }
    for review in ensemble.cluster_reviews:
        if (
            review.version != "gmail_temporal_candidate_ensemble_cluster_review_v1"
            or review.reason != "split_semantics_unresolved"
            or review.cluster_id not in cluster_candidates
            or review.cluster_id in review_cluster_ids
            or review.requires_defer is not True
            or review.routable is not False
            or accepted_ids.intersection(cluster_candidates[review.cluster_id])
        ):
            raise GmailTemporalReviewError("ensemble cluster review is invalid")
        review_cluster_ids.add(review.cluster_id)

    return _BatchAuthority(
        batch=expected_batch,
        page_plan=page_plan,
        ensemble=ensemble,
        candidates=frontier.candidates,
        cluster_candidates={
            key: tuple(value) for key, value in cluster_candidates.items()
        },
        cluster_expression=cluster_expression,
        candidate_cluster=candidate_cluster,
    )


def _artifacts_for_authority(
    authority: _BatchAuthority,
) -> tuple[GmailTemporalReviewArtifact, ...]:
    candidates = {item.candidate_id: item for item in authority.candidates}
    supported = set(authority.ensemble.verdict_set.supported_candidate_ids)
    artifacts: list[GmailTemporalReviewArtifact] = []
    for candidate_id in authority.page_plan.covered_candidate_ids:
        if candidate_id not in supported:
            continue
        candidate = candidates[candidate_id]
        cluster_id = authority.candidate_cluster[candidate_id]
        hypothesis = _review_hypothesis((candidate,))
        artifacts.append(
            GmailTemporalReviewArtifact(
                version="gmail_temporal_review_artifact_v1",
                artifact_id=f"supported:{candidate_id}",
                kind="supported_citation",
                evidence_status="supported",
                batch_fingerprint=authority.batch.manifest.batch_fingerprint,
                frontier_fingerprint=authority.page_plan.frontier_fingerprint,
                parent_cluster_id=cluster_id,
                candidate_ids=(candidate_id,),
                hypotheses=(hypothesis,),
            )
        )

    candidate_rank = {
        candidate_id: index
        for index, candidate_id in enumerate(authority.page_plan.covered_candidate_ids)
    }
    for uncertainty in authority.ensemble.verdict_set.uncertain_clusters:
        candidate_ids = tuple(
            sorted(
                uncertainty.plausible_candidate_ids,
                key=lambda value: candidate_rank[value],
            )
        )
        if not candidate_ids or any(
            authority.candidate_cluster[value] != uncertainty.cluster_id
            for value in candidate_ids
        ):
            raise GmailTemporalReviewError("uncertainty sidecar exceeds its cluster")
        by_signature: dict[
            tuple[str, str, str, str, str | None],
            list[GmailTemporalVerificationCandidate],
        ] = {}
        for candidate_id in candidate_ids:
            candidate = candidates[candidate_id]
            by_signature.setdefault(_candidate_signature(candidate), []).append(
                candidate
            )
        hypotheses = tuple(
            _review_hypothesis(tuple(by_signature[signature]))
            for signature in sorted(by_signature, key=_signature_sort_key)
        )
        artifacts.append(
            GmailTemporalReviewArtifact(
                version="gmail_temporal_review_artifact_v1",
                artifact_id=f"uncertainty:{uncertainty.cluster_id}",
                kind="uncertainty_sidecar",
                evidence_status="uncertain",
                batch_fingerprint=authority.batch.manifest.batch_fingerprint,
                frontier_fingerprint=authority.page_plan.frontier_fingerprint,
                parent_cluster_id=uncertainty.cluster_id,
                candidate_ids=candidate_ids,
                hypotheses=hypotheses,
            )
        )
    return tuple(artifacts)


def _cluster_reviews_for_authority(
    authority: _BatchAuthority,
) -> tuple[GmailTemporalReviewClusterReview, ...]:
    return tuple(
        GmailTemporalReviewClusterReview(
            version="gmail_temporal_review_cluster_review_v1",
            review_id=f"cluster_review:{review.cluster_id}",
            batch_fingerprint=authority.batch.manifest.batch_fingerprint,
            frontier_fingerprint=authority.page_plan.frontier_fingerprint,
            cluster_id=review.cluster_id,
            expression_id=authority.cluster_expression[review.cluster_id],
            candidate_ids=authority.cluster_candidates[review.cluster_id],
            reason="split_semantics_unresolved",
        )
        for review in authority.ensemble.cluster_reviews
    )


def _review_hypothesis(
    candidates: tuple[GmailTemporalVerificationCandidate, ...],
) -> GmailTemporalReviewHypothesis:
    if not candidates:
        raise GmailTemporalReviewError("review hypothesis cannot be empty")
    signature = _candidate_signature(candidates[0])
    if any(_candidate_signature(item) != signature for item in candidates[1:]):
        raise GmailTemporalReviewError("review hypothesis mixes semantic signatures")
    expression_id, relation, kind, lifecycle, normalized_value = signature
    candidate_ids = tuple(item.candidate_id for item in candidates)
    material = {
        "version": _HYPOTHESIS_VERSION,
        "signature": signature,
    }
    return GmailTemporalReviewHypothesis(
        version="gmail_temporal_review_hypothesis_v1",
        hypothesis_id="gtrh_" + hashlib.sha256(_canonical_bytes(material)).hexdigest(),
        expression_id=expression_id,
        subject_mention_ids=tuple(
            sorted({item.subject_mention_id for item in candidates})
        ),
        lifecycle_mention_ids=tuple(
            sorted(
                {
                    item.lifecycle_mention_id
                    for item in candidates
                    if item.lifecycle_mention_id is not None
                }
            )
        ),
        relation=relation,
        kind=kind,
        lifecycle=lifecycle,
        normalized_value=normalized_value,
        candidate_ids=candidate_ids,
        candidate_requires_defer=any(item.requires_defer for item in candidates),
    )


def _candidate_signature(
    candidate: GmailTemporalVerificationCandidate,
) -> tuple[str, str, str, str, str | None]:
    return (
        candidate.expression_id,
        candidate.relation,
        candidate.kind,
        candidate.lifecycle,
        candidate.normalized_value,
    )


def _signature_sort_key(value: tuple[str, str, str, str, str | None]) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _subject_alias_families(
    *,
    analysis: TemporalLeadAnalysis,
    batches: tuple[GmailTemporalSelectorBatch, ...],
    candidates: tuple[GmailTemporalVerificationCandidate, ...],
) -> dict[str, str]:
    subject_ids = tuple(sorted({item.subject_mention_id for item in candidates}))
    mentions = {item.mention_id: item for item in analysis.mentions}
    if any(value not in mentions for value in subject_ids):
        raise GmailTemporalReviewError("candidate subject mention is unknown")
    parent = {value: value for value in subject_ids}

    def find(value: str) -> str:
        while parent[value] != value:
            parent[value] = parent[parent[value]]
            value = parent[value]
        return value

    def union(first: str, second: str) -> None:
        first_root = find(first)
        second_root = find(second)
        if first_root != second_root:
            parent[max(first_root, second_root)] = min(first_root, second_root)

    batch_mention_ids = {
        batch.manifest.batch_fingerprint: set(batch.manifest.mention_ids)
        for batch in batches
    }
    for index, first_id in enumerate(subject_ids):
        first = mentions[first_id]
        for second_id in subject_ids[index + 1 :]:
            second = mentions[second_id]
            if first.start < second.end and second.start < first.end:
                union(first_id, second_id)
                continue
            for batch in batches:
                visible = batch_mention_ids[batch.manifest.batch_fingerprint]
                if first_id not in visible or second_id not in visible:
                    continue
                if (
                    classify_gmail_temporal_subject_pair(
                        first,
                        second,
                        batch=batch,
                    )
                    == "alias"
                ):
                    union(first_id, second_id)
                    break

    components: dict[str, list[str]] = {}
    for subject_id in subject_ids:
        components.setdefault(find(subject_id), []).append(subject_id)
    output: dict[str, str] = {}
    for values in components.values():
        material = {
            "analysis_fingerprint": analysis.snapshot_fingerprint,
            "subject_mention_ids": sorted(values),
        }
        family_id = "gtrsf_" + hashlib.sha256(_canonical_bytes(material)).hexdigest()
        for value in values:
            output[value] = family_id
    return output


def _structural_frames(
    *,
    text: str,
    analysis: TemporalLeadAnalysis,
) -> tuple[_StructuralFrame, ...]:
    expressions_by_segment: dict[tuple[str, str], list[TemporalExpression]] = {}
    for expression in sorted(
        analysis.expressions,
        key=lambda item: (item.start, item.end, item.expression_id),
    ):
        expressions_by_segment.setdefault(
            (expression.field, expression.segment_id),
            [],
        ).append(expression)

    frames: list[_StructuralFrame] = []
    consumed: set[str] = set()
    lifecycle_mentions = tuple(
        item
        for item in analysis.mentions
        if item.mention_type == "lifecycle" and item.lifecycle_role == "rescheduled"
    )
    for expressions in expressions_by_segment.values():
        for first, second in zip(expressions, expressions[1:]):
            connector = text[first.end : second.start]
            if _RESCHEDULE_CONNECTOR_RE.fullmatch(connector) is None:
                continue
            clause_start = _clause_start(text, first.start)
            prefix = text[clause_start : first.start]
            match = _RESCHEDULE_PREFIX_RE.search(prefix)
            if match is None:
                continue
            cue_start = clause_start + match.start()
            governed_lifecycles = tuple(
                item
                for item in lifecycle_mentions
                if item.field == first.field
                and item.segment_id == first.segment_id
                and cue_start <= item.start
                and item.end <= first.start
            )
            if len(governed_lifecycles) != 1:
                continue
            frame = _make_structural_frame(
                analysis_fingerprint=analysis.snapshot_fingerprint,
                kind="reschedule",
                source_start=cue_start,
                source_end=second.end,
                members=(
                    _StructuralMember(first.expression_id, "rescheduled_old", 1),
                    _StructuralMember(
                        second.expression_id,
                        "rescheduled_replacement",
                        2,
                    ),
                ),
            )
            frames.append(frame)
            consumed.update((first.expression_id, second.expression_id))

    for expressions in expressions_by_segment.values():
        index = 0
        while index < len(expressions) - 1:
            first = expressions[index]
            if first.expression_id in consumed:
                index += 1
                continue
            chain = [first]
            connectors: list[str] = []
            cursor = index + 1
            while cursor < len(expressions):
                previous = chain[-1]
                current = expressions[cursor]
                if current.expression_id in consumed:
                    break
                connector = text[previous.end : current.start]
                if (
                    _ALTERNATIVE_COMMA_RE.fullmatch(connector) is None
                    and _ALTERNATIVE_OR_RE.fullmatch(connector) is None
                ):
                    break
                connectors.append(connector)
                chain.append(current)
                cursor += 1
                if _ALTERNATIVE_OR_RE.fullmatch(connector) is not None:
                    break
            if len(chain) < 2 or not any(
                _ALTERNATIVE_OR_RE.fullmatch(value) is not None for value in connectors
            ):
                index += 1
                continue
            clause_start = _clause_start(text, first.start)
            prefix = text[clause_start : first.start]
            match = _ALTERNATIVE_PREFIX_RE.search(prefix)
            if match is None:
                index += 1
                continue
            source_start = clause_start + match.start()
            frame = _make_structural_frame(
                analysis_fingerprint=analysis.snapshot_fingerprint,
                kind="alternatives",
                source_start=source_start,
                source_end=chain[-1].end,
                members=tuple(
                    _StructuralMember(item.expression_id, "alternative", order)
                    for order, item in enumerate(chain, start=1)
                ),
            )
            frames.append(frame)
            consumed.update(item.expression_id for item in chain)
            index = cursor

    return tuple(
        sorted(
            frames, key=lambda item: (item.source_start, item.source_end, item.frame_id)
        )
    )


def _make_structural_frame(
    *,
    analysis_fingerprint: str,
    kind: Literal["alternatives", "reschedule"],
    source_start: int,
    source_end: int,
    members: tuple[_StructuralMember, ...],
) -> _StructuralFrame:
    material = {
        "version": _GROUP_VERSION,
        "analysis_fingerprint": analysis_fingerprint,
        "kind": kind,
        "source_start": source_start,
        "source_end": source_end,
        "members": [asdict(item) for item in members],
    }
    return _StructuralFrame(
        frame_id="gtrg_" + hashlib.sha256(_canonical_bytes(material)).hexdigest(),
        kind=kind,
        source_start=source_start,
        source_end=source_end,
        members=members,
    )


def _review_groups(
    *,
    analysis: TemporalLeadAnalysis,
    frames: tuple[_StructuralFrame, ...],
    artifacts: tuple[GmailTemporalReviewArtifact, ...],
    cluster_reviews: tuple[GmailTemporalReviewClusterReview, ...],
    candidates: Mapping[str, GmailTemporalVerificationCandidate],
    subject_families: Mapping[str, str],
) -> tuple[GmailTemporalReviewGroup, ...]:
    expressions = {item.expression_id: item for item in analysis.expressions}
    artifacts_by_expression: dict[str, list[GmailTemporalReviewArtifact]] = {}
    for artifact in artifacts:
        expression_ids = {item.expression_id for item in artifact.hypotheses}
        if len(expression_ids) != 1:
            raise GmailTemporalReviewError("one review artifact spans expressions")
        expression_id = next(iter(expression_ids))
        artifacts_by_expression.setdefault(expression_id, []).append(artifact)
    reviews_by_expression: dict[str, list[GmailTemporalReviewClusterReview]] = {}
    for review in cluster_reviews:
        reviews_by_expression.setdefault(review.expression_id, []).append(review)

    groups: list[GmailTemporalReviewGroup] = []
    structurally_grouped_artifacts: set[str] = set()
    for frame in frames:
        projected_members: list[GmailTemporalReviewGroupMember] = []
        reasons: list[str] = []
        family_sets: list[set[str]] = []
        missing = False
        conflicted = False
        for structural_member in frame.members:
            expression_id = structural_member.expression_id
            member_artifacts = tuple(artifacts_by_expression.get(expression_id, ()))
            member_reviews = tuple(reviews_by_expression.get(expression_id, ()))
            artifact_ids = tuple(item.artifact_id for item in member_artifacts)
            review_ids = tuple(item.review_id for item in member_reviews)
            family_ids = _artifact_subject_family_ids(
                member_artifacts,
                subject_families,
            )
            member_reasons: list[str] = []
            if not member_artifacts:
                missing = True
                state: ReviewGroupMemberState = "missing"
                member_reasons.append(
                    "split_semantics_unresolved"
                    if member_reviews
                    else "no_review_artifact"
                )
            else:
                if len(member_artifacts) > 1:
                    member_reasons.append("multiple_review_artifacts")
                if len(family_ids) != 1:
                    member_reasons.append("subject_alias_family_unresolved")
                if member_reviews:
                    member_reasons.append("split_semantics_unresolved")
                if member_reasons:
                    conflicted = True
                    state = "conflicted"
                else:
                    state = "present"
                    family_sets.append(set(family_ids))
            structurally_grouped_artifacts.update(artifact_ids)
            projected_members.append(
                _group_member(
                    group_id=frame.frame_id,
                    expression_id=expression_id,
                    role=structural_member.role,
                    source_order=structural_member.source_order,
                    state=state,
                    artifact_ids=artifact_ids,
                    cluster_review_ids=review_ids,
                    subject_family_ids=family_ids,
                    reasons=tuple(member_reasons),
                )
            )

        subject_family_id: str | None = None
        if family_sets:
            common = set.intersection(*family_sets)
            if len(common) == 1 and all(values == common for values in family_sets):
                subject_family_id = next(iter(common))
            else:
                conflicted = True
                reasons.append("incompatible_subject_alias_families")
        if conflicted:
            coverage: ReviewGroupCoverage = "conflicted"
        elif missing:
            coverage = "incomplete"
        else:
            coverage = "complete"
        groups.append(
            GmailTemporalReviewGroup(
                version="gmail_temporal_review_group_v1",
                group_id=frame.frame_id,
                kind=frame.kind,
                coverage=coverage,
                source_start=frame.source_start,
                source_end=frame.source_end,
                subject_family_id=subject_family_id if coverage == "complete" else None,
                members=tuple(projected_members),
                reasons=tuple(sorted(set(reasons))),
            )
        )

    for artifact in artifacts:
        if artifact.artifact_id in structurally_grouped_artifacts:
            continue
        expression_id = artifact.hypotheses[0].expression_id
        expression = expressions[expression_id]
        family_ids = _artifact_subject_family_ids((artifact,), subject_families)
        conflicted = len(family_ids) != 1
        group_id = _derived_group_id(
            analysis.snapshot_fingerprint,
            "single",
            artifact.artifact_id,
        )
        groups.append(
            GmailTemporalReviewGroup(
                version="gmail_temporal_review_group_v1",
                group_id=group_id,
                kind="single",
                coverage="conflicted" if conflicted else "complete",
                source_start=expression.start,
                source_end=expression.end,
                subject_family_id=(family_ids[0] if len(family_ids) == 1 else None),
                members=(
                    _group_member(
                        group_id=group_id,
                        expression_id=expression_id,
                        role="independent",
                        source_order=None,
                        state="conflicted" if conflicted else "present",
                        artifact_ids=(artifact.artifact_id,),
                        cluster_review_ids=(),
                        subject_family_ids=family_ids,
                        reasons=(
                            ("subject_alias_family_unresolved",) if conflicted else ()
                        ),
                    ),
                ),
                reasons=(("subject_alias_family_unresolved",) if conflicted else ()),
            )
        )

    for review in cluster_reviews:
        expression = expressions[review.expression_id]
        family_ids = tuple(
            sorted(
                {
                    subject_families[candidates[candidate_id].subject_mention_id]
                    for candidate_id in review.candidate_ids
                }
            )
        )
        group_id = _derived_group_id(
            analysis.snapshot_fingerprint,
            "split_semantics",
            review.review_id,
        )
        groups.append(
            GmailTemporalReviewGroup(
                version="gmail_temporal_review_group_v1",
                group_id=group_id,
                kind="split_semantics",
                coverage="conflicted",
                source_start=expression.start,
                source_end=expression.end,
                subject_family_id=family_ids[0] if len(family_ids) == 1 else None,
                members=(
                    _group_member(
                        group_id=group_id,
                        expression_id=review.expression_id,
                        role="unresolved",
                        source_order=None,
                        state="conflicted",
                        artifact_ids=(),
                        cluster_review_ids=(review.review_id,),
                        subject_family_ids=family_ids,
                        reasons=("split_semantics_unresolved",),
                    ),
                ),
                reasons=("split_semantics_unresolved",),
            )
        )

    kind_rank = {
        "alternatives": 0,
        "reschedule": 1,
        "single": 2,
        "split_semantics": 3,
    }
    return tuple(
        sorted(
            groups,
            key=lambda item: (
                item.source_start,
                item.source_end,
                kind_rank[item.kind],
                item.group_id,
            ),
        )
    )


def _artifact_subject_family_ids(
    artifacts: tuple[GmailTemporalReviewArtifact, ...],
    subject_families: Mapping[str, str],
) -> tuple[str, ...]:
    return tuple(
        sorted(
            {
                subject_families[subject_id]
                for artifact in artifacts
                for hypothesis in artifact.hypotheses
                for subject_id in hypothesis.subject_mention_ids
            }
        )
    )


def _group_member(
    *,
    group_id: str,
    expression_id: str,
    role: ReviewGroupRole,
    source_order: int | None,
    state: ReviewGroupMemberState,
    artifact_ids: tuple[str, ...],
    cluster_review_ids: tuple[str, ...],
    subject_family_ids: tuple[str, ...],
    reasons: tuple[str, ...],
) -> GmailTemporalReviewGroupMember:
    material = {
        "version": _GROUP_MEMBER_VERSION,
        "group_id": group_id,
        "expression_id": expression_id,
        "role": role,
        "source_order": source_order,
    }
    return GmailTemporalReviewGroupMember(
        version="gmail_temporal_review_group_member_v1",
        member_id="gtrgm_" + hashlib.sha256(_canonical_bytes(material)).hexdigest(),
        expression_id=expression_id,
        role=role,
        source_order=source_order,
        state=state,
        artifact_ids=artifact_ids,
        cluster_review_ids=cluster_review_ids,
        subject_family_ids=subject_family_ids,
        reasons=reasons,
    )


def _derived_group_id(analysis_fingerprint: str, kind: str, authority_id: str) -> str:
    material = {
        "version": _GROUP_VERSION,
        "analysis_fingerprint": analysis_fingerprint,
        "kind": kind,
        "authority_id": authority_id,
    }
    return "gtrg_" + hashlib.sha256(_canonical_bytes(material)).hexdigest()


def _clause_start(text: str, position: int) -> int:
    boundaries = [text.rfind(value, 0, position) for value in ".!?;\r\n"]
    return max(boundaries) + 1


def _expression_rank(analysis: TemporalLeadAnalysis, expression_id: str) -> int:
    for index, expression in enumerate(analysis.expressions):
        if expression.expression_id == expression_id:
            return index
    raise GmailTemporalReviewError("review output references an unknown expression")


def _projection_material(
    projection: GmailTemporalReviewProjection,
) -> dict[str, object]:
    return {
        "version": projection.version,
        "analysis_fingerprint": projection.analysis_fingerprint,
        "source_sha256": projection.source_sha256,
        "batch_plan_fingerprint": projection.batch_plan_fingerprint,
        "ensemble_policy_fingerprint": projection.ensemble_policy_fingerprint,
        "grouping_policy_fingerprint": projection.grouping_policy_fingerprint,
        "independent_invocations_verified": (
            projection.independent_invocations_verified
        ),
        "component_evidence_fingerprints": list(
            projection.component_evidence_fingerprints
        ),
        "artifacts": [_jsonable(asdict(item)) for item in projection.artifacts],
        "cluster_reviews": [
            _jsonable(asdict(item)) for item in projection.cluster_reviews
        ],
        "groups": [_jsonable(asdict(item)) for item in projection.groups],
        "complete": projection.complete,
        "requires_defer": projection.requires_defer,
        "routable": projection.routable,
    }


def _validate_projection(projection: GmailTemporalReviewProjection) -> None:
    if (
        not isinstance(projection, GmailTemporalReviewProjection)
        or projection.version != _PROJECTION_VERSION
        or projection.complete is not True
        or projection.requires_defer is not True
        or projection.routable is not False
        or projection.ensemble_policy_fingerprint
        != gmail_temporal_candidate_ensemble_policy_fingerprint()
        or projection.grouping_policy_fingerprint
        != gmail_temporal_review_grouping_policy_fingerprint()
        or projection.independent_invocations_verified is not False
        or not isinstance(projection.component_evidence_fingerprints, tuple)
        or len(projection.component_evidence_fingerprints) not in {0, 3}
        or any(
            not isinstance(value, str) or not value
            for value in projection.component_evidence_fingerprints
        )
    ):
        raise GmailTemporalReviewError("review projection structure is invalid")
    expected_fingerprint = (
        "gtrp_"
        + hashlib.sha256(_canonical_bytes(_projection_material(projection))).hexdigest()
    )
    if projection.projection_fingerprint != expected_fingerprint:
        raise GmailTemporalReviewError("review projection fingerprint is stale")

    artifacts = {item.artifact_id: item for item in projection.artifacts}
    reviews = {item.review_id: item for item in projection.cluster_reviews}
    groups = {item.group_id: item for item in projection.groups}
    if (
        len(artifacts) != len(projection.artifacts)
        or len(reviews) != len(projection.cluster_reviews)
        or len(groups) != len(projection.groups)
    ):
        raise GmailTemporalReviewError("review projection contains duplicate IDs")
    if any(not _artifact_is_valid(item) for item in projection.artifacts):
        raise GmailTemporalReviewError("review artifact structure is invalid")
    if any(not _cluster_review_is_valid(item) for item in projection.cluster_reviews):
        raise GmailTemporalReviewError("cluster review structure is invalid")

    artifact_group_counts = {artifact_id: 0 for artifact_id in artifacts}
    split_review_counts = {review_id: 0 for review_id in reviews}
    for group in projection.groups:
        if not _group_is_valid(group, artifacts=artifacts, reviews=reviews):
            raise GmailTemporalReviewError("review group structure is invalid")
        for member in group.members:
            for artifact_id in member.artifact_ids:
                artifact_group_counts[artifact_id] += 1
            if group.kind == "split_semantics":
                for review_id in member.cluster_review_ids:
                    split_review_counts[review_id] += 1
    if any(value != 1 for value in artifact_group_counts.values()):
        raise GmailTemporalReviewError(
            "every review artifact must belong to exactly one semantic group"
        )
    if any(value != 1 for value in split_review_counts.values()):
        raise GmailTemporalReviewError(
            "every split semantic review must have exactly one review group"
        )


def _artifact_is_valid(item: GmailTemporalReviewArtifact) -> bool:
    candidate_ids = tuple(item.candidate_ids)
    hypotheses = tuple(item.hypotheses)
    if (
        item.version != _ARTIFACT_VERSION
        or item.kind not in {"supported_citation", "uncertainty_sidecar"}
        or item.evidence_status
        != ("supported" if item.kind == "supported_citation" else "uncertain")
        or not item.batch_fingerprint
        or not item.frontier_fingerprint
        or not item.parent_cluster_id
        or not candidate_ids
        or len(candidate_ids) != len(set(candidate_ids))
        or not hypotheses
        or item.candidate_authorization is not True
        or item.requires_defer is not True
        or item.routable is not False
        or item.kind == "supported_citation"
        and (
            len(candidate_ids) != 1
            or len(hypotheses) != 1
            or item.artifact_id != f"supported:{candidate_ids[0]}"
        )
        or item.kind == "uncertainty_sidecar"
        and item.artifact_id != f"uncertainty:{item.parent_cluster_id}"
    ):
        return False

    covered_candidate_ids: list[str] = []
    expression_ids: set[str] = set()
    hypothesis_ids: set[str] = set()
    for hypothesis in hypotheses:
        signature = (
            hypothesis.expression_id,
            hypothesis.relation,
            hypothesis.kind,
            hypothesis.lifecycle,
            hypothesis.normalized_value,
        )
        expected_hypothesis_id = (
            "gtrh_"
            + hashlib.sha256(
                _canonical_bytes(
                    {
                        "version": _HYPOTHESIS_VERSION,
                        "signature": signature,
                    }
                )
            ).hexdigest()
        )
        if (
            hypothesis.version != _HYPOTHESIS_VERSION
            or hypothesis.hypothesis_id != expected_hypothesis_id
            or hypothesis.hypothesis_id in hypothesis_ids
            or not hypothesis.expression_id
            or not hypothesis.subject_mention_ids
            or len(hypothesis.subject_mention_ids)
            != len(set(hypothesis.subject_mention_ids))
            or len(hypothesis.lifecycle_mention_ids)
            != len(set(hypothesis.lifecycle_mention_ids))
            or not hypothesis.candidate_ids
            or len(hypothesis.candidate_ids) != len(set(hypothesis.candidate_ids))
            or not isinstance(hypothesis.candidate_requires_defer, bool)
            or hypothesis.requires_defer is not True
            or hypothesis.routable is not False
        ):
            return False
        hypothesis_ids.add(hypothesis.hypothesis_id)
        expression_ids.add(hypothesis.expression_id)
        covered_candidate_ids.extend(hypothesis.candidate_ids)
    return (
        len(expression_ids) == 1
        and len(covered_candidate_ids) == len(set(covered_candidate_ids))
        and set(covered_candidate_ids) == set(candidate_ids)
    )


def _cluster_review_is_valid(item: GmailTemporalReviewClusterReview) -> bool:
    return bool(
        item.version == _CLUSTER_REVIEW_VERSION
        and item.review_id == f"cluster_review:{item.cluster_id}"
        and item.batch_fingerprint
        and item.frontier_fingerprint
        and item.cluster_id
        and item.expression_id
        and item.candidate_ids
        and len(item.candidate_ids) == len(set(item.candidate_ids))
        and item.reason == "split_semantics_unresolved"
        and item.candidate_authorization is False
        and item.requires_defer is True
        and item.routable is False
    )


def _group_is_valid(
    group: GmailTemporalReviewGroup,
    *,
    artifacts: Mapping[str, GmailTemporalReviewArtifact],
    reviews: Mapping[str, GmailTemporalReviewClusterReview],
) -> bool:
    if (
        group.version != _GROUP_VERSION
        or group.kind not in {"single", "alternatives", "reschedule", "split_semantics"}
        or group.coverage not in {"complete", "incomplete", "conflicted"}
        or not group.members
        or group.source_start < 0
        or group.source_end <= group.source_start
        or len(group.reasons) != len(set(group.reasons))
        or group.candidate_authorization is not False
        or group.requires_defer is not True
        or group.routable is not False
        or any(
            not _group_member_is_valid(
                group.group_id,
                member,
                group_kind=group.kind,
                artifacts=artifacts,
                reviews=reviews,
            )
            for member in group.members
        )
    ):
        return False
    roles = tuple(member.role for member in group.members)
    orders = tuple(member.source_order for member in group.members)
    if group.kind == "single" and not (
        len(group.members) == 1 and roles == ("independent",) and orders == (None,)
    ):
        return False
    if group.kind == "alternatives" and not (
        len(group.members) >= 2
        and set(roles) == {"alternative"}
        and orders == tuple(range(1, len(group.members) + 1))
    ):
        return False
    if group.kind == "reschedule" and not (
        roles == ("rescheduled_old", "rescheduled_replacement") and orders == (1, 2)
    ):
        return False
    if group.kind == "split_semantics" and not (
        len(group.members) == 1
        and roles == ("unresolved",)
        and orders == (None,)
        and group.coverage == "conflicted"
        and group.members[0].state == "conflicted"
        and not group.members[0].artifact_ids
        and len(group.members[0].cluster_review_ids) == 1
        and group.members[0].reasons == ("split_semantics_unresolved",)
        and group.reasons == ("split_semantics_unresolved",)
    ):
        return False
    states = {member.state for member in group.members}
    if group.coverage == "complete":
        return (
            states == {"present"}
            and group.subject_family_id is not None
            and not group.reasons
            and all(
                member.subject_family_ids == (group.subject_family_id,)
                for member in group.members
            )
        )
    if group.coverage == "incomplete":
        return (
            "missing" in states
            and "conflicted" not in states
            and group.subject_family_id is None
        )
    return "conflicted" in states or bool(group.reasons)


def _group_member_is_valid(
    group_id: str,
    member: GmailTemporalReviewGroupMember,
    *,
    group_kind: ReviewGroupKind,
    artifacts: Mapping[str, GmailTemporalReviewArtifact],
    reviews: Mapping[str, GmailTemporalReviewClusterReview],
) -> bool:
    expected_member_id = (
        "gtrgm_"
        + hashlib.sha256(
            _canonical_bytes(
                {
                    "version": _GROUP_MEMBER_VERSION,
                    "group_id": group_id,
                    "expression_id": member.expression_id,
                    "role": member.role,
                    "source_order": member.source_order,
                }
            )
        ).hexdigest()
    )
    if (
        member.version != _GROUP_MEMBER_VERSION
        or member.member_id != expected_member_id
        or not member.expression_id
        or member.role
        not in {
            "independent",
            "alternative",
            "rescheduled_old",
            "rescheduled_replacement",
            "unresolved",
        }
        or member.state not in {"present", "missing", "conflicted"}
        or len(member.artifact_ids) != len(set(member.artifact_ids))
        or len(member.cluster_review_ids) != len(set(member.cluster_review_ids))
        or len(member.subject_family_ids) != len(set(member.subject_family_ids))
        or len(member.reasons) != len(set(member.reasons))
        or any(value not in artifacts for value in member.artifact_ids)
        or any(value not in reviews for value in member.cluster_review_ids)
        or any(
            any(
                hypothesis.expression_id != member.expression_id
                for hypothesis in artifacts[value].hypotheses
            )
            for value in member.artifact_ids
        )
        or any(
            reviews[value].expression_id != member.expression_id
            for value in member.cluster_review_ids
        )
        or member.candidate_authorization is not False
        or member.requires_defer is not True
        or member.routable is not False
    ):
        return False

    if group_kind == "split_semantics":
        return (
            member.state == "conflicted"
            and not member.artifact_ids
            and len(member.cluster_review_ids) == 1
            and member.reasons == ("split_semantics_unresolved",)
        )
    if group_kind == "single":
        if len(member.artifact_ids) != 1 or member.cluster_review_ids:
            return False
        if member.state == "present":
            return len(member.subject_family_ids) == 1 and not member.reasons
        return member.state == "conflicted" and member.reasons == (
            "subject_alias_family_unresolved",
        )

    allowed_structural_reasons = {
        "multiple_review_artifacts",
        "no_review_artifact",
        "split_semantics_unresolved",
        "subject_alias_family_unresolved",
    }
    if not set(member.reasons) <= allowed_structural_reasons:
        return False
    has_split_review = bool(member.cluster_review_ids)
    if has_split_review != ("split_semantics_unresolved" in member.reasons):
        return False
    if member.state == "present":
        return (
            len(member.artifact_ids) == 1
            and not member.cluster_review_ids
            and len(member.subject_family_ids) == 1
            and not member.reasons
        )
    if member.state == "missing":
        return (
            not member.artifact_ids
            and not member.subject_family_ids
            and member.reasons
            == (
                "split_semantics_unresolved"
                if has_split_review
                else "no_review_artifact",
            )
        )
    return (
        bool(member.artifact_ids)
        and bool(member.reasons)
        and "no_review_artifact" not in member.reasons
    )


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    return value


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
