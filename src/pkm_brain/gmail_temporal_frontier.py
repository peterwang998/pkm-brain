from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Literal

from .gmail_temporal_batching import (
    GmailTemporalBatchAuthorityError,
    GmailTemporalSelectorBatch,
    VerifiedGmailTemporalBatchCitation,
    validate_gmail_temporal_batch_citation,
    validate_gmail_temporal_batch_manifest,
)
from .gmail_temporal_leads import TemporalLeadAnalysis
from .gmail_temporal_selection import (
    GMAIL_TEMPORAL_SUBJECT_TYPES,
    GmailTemporalSelectionError,
    SelectedTemporalAssociation,
    validate_gmail_temporal_selection,
)


_FRONTIER_VERSION = "gmail_temporal_candidate_frontier_v1"
_CANDIDATE_VERSION = "gmail_temporal_verification_candidate_v1"
_PAGE_PLAN_VERSION = "gmail_temporal_candidate_page_plan_v1"
_PAGE_VERSION = "gmail_temporal_candidate_page_v1"
_VERDICT_SET_VERSION = "gmail_temporal_candidate_verdict_set_v1"
_VERDICTS = {"supported", "unsupported", "uncertain"}


class GmailTemporalFrontierError(ValueError):
    """Raised when a verification frontier exceeds its evidence authority."""


@dataclass(frozen=True)
class GmailTemporalVerificationCandidate:
    """One deterministic expression-subject binding for semantic verification."""

    version: Literal["gmail_temporal_verification_candidate_v1"]
    candidate_id: str
    binding_id: str
    expression_id: str
    subject_mention_id: str
    lifecycle_mention_id: str | None
    selected_lead_id: str | None
    relation: str
    kind: str
    lifecycle: str
    normalized_value: str | None
    blockers: tuple[str, ...]
    risk_features: tuple[str, ...]
    repair_flags: tuple[str, ...]
    requires_defer: bool
    supporting_lead_present: bool
    routable: Literal[False] = False


@dataclass(frozen=True)
class GmailTemporalCandidateFrontier:
    """The complete validator-backed candidate set for one bounded packet."""

    version: Literal["gmail_temporal_candidate_frontier_v1"]
    frontier_fingerprint: str
    batch_fingerprint: str
    analysis_fingerprint: str
    candidates: tuple[GmailTemporalVerificationCandidate, ...]
    complete: bool
    omitted_mention_count: int
    omitted_candidate_mention_count: int
    routable: Literal[False] = False


@dataclass(frozen=True)
class GmailTemporalCandidateCluster:
    """Reducer-equivalent subjects presented as one model decision unit."""

    cluster_id: str
    decision_unit_id: str
    expression_id: str
    subject_mention_ids: tuple[str, ...]
    candidate_ids: tuple[str, ...]
    routable: Literal[False] = False


@dataclass(frozen=True)
class GmailTemporalCandidatePage:
    """At most four alias-aware bindings sharing one expression packet."""

    version: Literal["gmail_temporal_candidate_page_v1"]
    sequence: int
    page_fingerprint: str
    frontier_fingerprint: str
    clusters: tuple[GmailTemporalCandidateCluster, ...]
    routable: Literal[False] = False


@dataclass(frozen=True)
class GmailTemporalCandidatePagePlan:
    """Lossless paging for a constrained candidate-by-candidate verifier."""

    version: Literal["gmail_temporal_candidate_page_plan_v1"]
    plan_fingerprint: str
    frontier_fingerprint: str
    pages: tuple[GmailTemporalCandidatePage, ...]
    covered_candidate_ids: tuple[str, ...]
    max_clusters_per_page: int
    max_candidates_per_page: int
    max_payload_bytes: int
    complete: bool
    routable: Literal[False] = False


@dataclass(frozen=True)
class GmailTemporalCandidateVerdict:
    """One model verdict for exactly one candidate shown on one page."""

    candidate_id: str
    verdict: Literal["supported", "unsupported", "uncertain"]
    routable: Literal[False] = False


@dataclass(frozen=True)
class GmailTemporalCandidatePageVerdicts:
    """Complete candidate verdict coverage for one immutable page."""

    frontier_fingerprint: str
    page_fingerprint: str
    verdicts: tuple[GmailTemporalCandidateVerdict, ...]
    routable: Literal[False] = False


@dataclass(frozen=True)
class GmailTemporalCandidateVerdictSet:
    """Plan-complete, page-authorized verdicts for deterministic projection."""

    version: Literal["gmail_temporal_candidate_verdict_set_v1"]
    plan_fingerprint: str
    frontier_fingerprint: str
    supported_candidate_ids: tuple[str, ...]
    uncertain_candidate_ids: tuple[str, ...]
    unsupported_candidate_count: int
    supported_citations: tuple[VerifiedGmailTemporalBatchCitation, ...]
    uncertain_citations: tuple[VerifiedGmailTemporalBatchCitation, ...]
    page_count: int
    complete: bool
    requires_defer: bool
    routable: Literal[False] = False


def build_gmail_temporal_candidate_frontier(
    *,
    analysis: TemporalLeadAnalysis,
    batch: GmailTemporalSelectorBatch,
) -> GmailTemporalCandidateFrontier:
    """Enumerate every independently valid binding exposed by one packet.

    The model never has to invent an expression-subject pair. It can instead
    classify this finite list as supported, unsupported, or uncertain. Candidate
    semantics are derived by the same deterministic validator used downstream.
    """

    validate_gmail_temporal_batch_manifest(batch)
    if batch.manifest.analysis_fingerprint != analysis.snapshot_fingerprint:
        raise GmailTemporalFrontierError(
            "batch does not match the current temporal analysis"
        )

    lifecycle_ids = tuple(
        item.mention_id
        for item in batch.mentions
        if item.mention_type == "lifecycle"
    )
    candidates: list[GmailTemporalVerificationCandidate] = []
    for expression in batch.expressions:
        for mention in batch.mentions:
            base = _candidate(
                analysis=analysis,
                batch=batch,
                expression_id=expression.expression_id,
                subject_mention_id=mention.mention_id,
                lifecycle_mention_id=None,
            )
            if base is None:
                continue
            candidates.append(base)
            for lifecycle_id in lifecycle_ids:
                lifecycle_candidate = _candidate(
                    analysis=analysis,
                    batch=batch,
                    expression_id=expression.expression_id,
                    subject_mention_id=mention.mention_id,
                    lifecycle_mention_id=lifecycle_id,
                )
                if lifecycle_candidate is not None:
                    candidates.append(lifecycle_candidate)

    ordered = tuple(
        sorted(
            candidates,
            key=lambda item: (
                item.expression_id,
                item.subject_mention_id,
                item.lifecycle_mention_id or "",
                item.selected_lead_id or "",
                item.candidate_id,
            ),
        )
    )
    omitted_candidate_mention_count = _omitted_candidate_mention_count(
        analysis,
        batch,
    )
    material = {
        "version": _FRONTIER_VERSION,
        "batch_fingerprint": batch.manifest.batch_fingerprint,
        "analysis_fingerprint": analysis.snapshot_fingerprint,
        "candidates": [asdict(item) for item in ordered],
        "complete": omitted_candidate_mention_count == 0,
        "omitted_mention_count": batch.omitted_mention_count,
        "omitted_candidate_mention_count": omitted_candidate_mention_count,
    }
    fingerprint = "gtf_" + hashlib.sha256(
        json.dumps(
            material,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    return GmailTemporalCandidateFrontier(
        version="gmail_temporal_candidate_frontier_v1",
        frontier_fingerprint=fingerprint,
        batch_fingerprint=batch.manifest.batch_fingerprint,
        analysis_fingerprint=analysis.snapshot_fingerprint,
        candidates=ordered,
        complete=omitted_candidate_mention_count == 0,
        omitted_mention_count=batch.omitted_mention_count,
        omitted_candidate_mention_count=omitted_candidate_mention_count,
    )


def gmail_temporal_candidate_frontier_payload(
    frontier: GmailTemporalCandidateFrontier,
) -> str:
    """Return canonical JSON for a candidate-by-candidate verifier."""

    return json.dumps(
        asdict(frontier),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def plan_gmail_temporal_candidate_pages(
    *,
    analysis: TemporalLeadAnalysis,
    batch: GmailTemporalSelectorBatch,
    max_clusters_per_page: int = 4,
    max_candidates_per_page: int = 12,
    max_payload_bytes: int = 12_000,
) -> GmailTemporalCandidatePagePlan:
    """Alias-cluster and losslessly page a packet's full candidate frontier.

    Four clusters is the historical Pareto point, but overflow is paged rather
    than dropped. This preserves recall for dense messages while keeping each
    semantic decision small and explicit.
    """

    for name, value in (
        ("max_clusters_per_page", max_clusters_per_page),
        ("max_candidates_per_page", max_candidates_per_page),
        ("max_payload_bytes", max_payload_bytes),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"{name} must be a positive integer")
    frontier = build_gmail_temporal_candidate_frontier(
        analysis=analysis,
        batch=batch,
    )
    clusters = _alias_clusters(analysis, batch, frontier)
    pages: list[GmailTemporalCandidatePage] = []
    by_expression: dict[str, list[GmailTemporalCandidateCluster]] = {}
    for cluster in clusters:
        by_expression.setdefault(cluster.expression_id, []).append(cluster)
    for expression_clusters in by_expression.values():
        pending = [
            fragment
            for cluster in expression_clusters
            for fragment in _candidate_count_fragments(
                cluster,
                max_candidates=max_candidates_per_page,
            )
        ]
        current: list[GmailTemporalCandidateCluster] = []
        while pending:
            fragment = pending.pop(0)
            proposed = (*current, fragment)
            page = _make_page(
                frontier,
                proposed,
                sequence=len(pages) + 1,
            )
            proposed_candidate_count = sum(
                len(item.candidate_ids) for item in proposed
            )
            fits = (
                len(proposed) <= max_clusters_per_page
                and proposed_candidate_count <= max_candidates_per_page
                and len(_page_payload_bytes(frontier, page)) <= max_payload_bytes
            )
            if fits:
                current.append(fragment)
                continue
            if current:
                finalized = _make_page(
                    frontier,
                    tuple(current),
                    sequence=len(pages) + 1,
                )
                pages.append(finalized)
                current = []
                pending.insert(0, fragment)
                continue
            if len(fragment.candidate_ids) == 1:
                raise GmailTemporalFrontierError(
                    "one verification candidate exceeds the page byte bound"
                )
            midpoint = len(fragment.candidate_ids) // 2
            pending[0:0] = [
                _cluster_fragment(fragment, fragment.candidate_ids[:midpoint]),
                _cluster_fragment(fragment, fragment.candidate_ids[midpoint:]),
            ]
        if current:
            pages.append(
                _make_page(
                    frontier,
                    tuple(current),
                    sequence=len(pages) + 1,
                )
            )
    for page in pages:
        _validate_page_bounds(
            frontier,
            page,
            max_clusters=max_clusters_per_page,
            max_candidates=max_candidates_per_page,
            max_payload_bytes=max_payload_bytes,
        )
    covered = tuple(
        candidate_id
        for page in pages
        for cluster in page.clusters
        for candidate_id in cluster.candidate_ids
    )
    expected = {item.candidate_id for item in frontier.candidates}
    if len(covered) != len(set(covered)) or set(covered) != expected:
        raise GmailTemporalFrontierError(
            "candidate page plan is incomplete or duplicated"
        )
    plan_material = {
        "version": _PAGE_PLAN_VERSION,
        "frontier_fingerprint": frontier.frontier_fingerprint,
        "page_fingerprints": [item.page_fingerprint for item in pages],
        "covered_candidate_ids": covered,
        "max_clusters_per_page": max_clusters_per_page,
        "max_candidates_per_page": max_candidates_per_page,
        "max_payload_bytes": max_payload_bytes,
        "complete": frontier.complete,
    }
    return GmailTemporalCandidatePagePlan(
        version="gmail_temporal_candidate_page_plan_v1",
        plan_fingerprint="gtfpp_"
        + hashlib.sha256(_canonical_bytes(plan_material)).hexdigest(),
        frontier_fingerprint=frontier.frontier_fingerprint,
        pages=tuple(pages),
        covered_candidate_ids=covered,
        max_clusters_per_page=max_clusters_per_page,
        max_candidates_per_page=max_candidates_per_page,
        max_payload_bytes=max_payload_bytes,
        complete=frontier.complete,
    )


def gmail_temporal_candidate_page_payload(
    *,
    frontier: GmailTemporalCandidateFrontier,
    page: GmailTemporalCandidatePage,
) -> str:
    """Return canonical verifier JSON for one page and its candidate details."""

    _validate_page_integrity(frontier, page)
    return _page_payload_bytes(frontier, page).decode("utf-8")


def _validate_gmail_temporal_candidate_choice(
    *,
    analysis: TemporalLeadAnalysis,
    batch: GmailTemporalSelectorBatch,
    frontier_fingerprint: str,
    candidate_id: str,
) -> VerifiedGmailTemporalBatchCitation:
    """Resolve one model-cited candidate ID back to an authorized citation."""

    frontier = build_gmail_temporal_candidate_frontier(
        analysis=analysis,
        batch=batch,
    )
    if frontier_fingerprint != frontier.frontier_fingerprint:
        raise GmailTemporalFrontierError("candidate frontier fingerprint is stale")
    matches = [
        item for item in frontier.candidates if item.candidate_id == candidate_id
    ]
    if len(matches) != 1:
        raise GmailTemporalFrontierError("candidate ID is unknown or duplicated")
    candidate = matches[0]
    return validate_gmail_temporal_batch_citation(
        batch,
        batch_fingerprint=batch.manifest.batch_fingerprint,
        expression_id=candidate.expression_id,
        subject_mention_id=candidate.subject_mention_id,
        lifecycle_mention_id=candidate.lifecycle_mention_id,
        selected_lead_id=candidate.selected_lead_id,
    )


def validate_gmail_temporal_candidate_page_choice(
    *,
    analysis: TemporalLeadAnalysis,
    batch: GmailTemporalSelectorBatch,
    page: GmailTemporalCandidatePage,
    frontier_fingerprint: str,
    page_fingerprint: str,
    candidate_id: str,
) -> VerifiedGmailTemporalBatchCitation:
    """Resolve a choice only when it appeared on the cited immutable page."""

    frontier = build_gmail_temporal_candidate_frontier(
        analysis=analysis,
        batch=batch,
    )
    _validate_page_integrity(frontier, page)
    if frontier_fingerprint != frontier.frontier_fingerprint:
        raise GmailTemporalFrontierError("candidate frontier fingerprint is stale")
    if page_fingerprint != page.page_fingerprint:
        raise GmailTemporalFrontierError("candidate page fingerprint is stale")
    allowed = {
        value
        for cluster in page.clusters
        for value in cluster.candidate_ids
    }
    if candidate_id not in allowed:
        raise GmailTemporalFrontierError(
            "candidate ID was not presented on the cited page"
        )
    return _validate_gmail_temporal_candidate_choice(
        analysis=analysis,
        batch=batch,
        frontier_fingerprint=frontier_fingerprint,
        candidate_id=candidate_id,
    )


def validate_gmail_temporal_candidate_verdict_set(
    *,
    analysis: TemporalLeadAnalysis,
    batch: GmailTemporalSelectorBatch,
    plan: GmailTemporalCandidatePagePlan,
    rows: tuple[GmailTemporalCandidatePageVerdicts, ...],
) -> GmailTemporalCandidateVerdictSet:
    """Require an exact verdict for every candidate on every planned page."""

    if not isinstance(plan, GmailTemporalCandidatePagePlan):
        raise GmailTemporalFrontierError("candidate page plan is invalid")
    expected_plan = plan_gmail_temporal_candidate_pages(
        analysis=analysis,
        batch=batch,
        max_clusters_per_page=plan.max_clusters_per_page,
        max_candidates_per_page=plan.max_candidates_per_page,
        max_payload_bytes=plan.max_payload_bytes,
    )
    if plan != expected_plan:
        raise GmailTemporalFrontierError("candidate page plan is stale or mutated")
    if not isinstance(rows, tuple) or any(
        not isinstance(item, GmailTemporalCandidatePageVerdicts) for item in rows
    ):
        raise GmailTemporalFrontierError("candidate verdict rows are invalid")
    if any(
        not isinstance(item.frontier_fingerprint, str)
        or not isinstance(item.page_fingerprint, str)
        or not isinstance(item.verdicts, tuple)
        or any(
            not isinstance(verdict, GmailTemporalCandidateVerdict)
            or not isinstance(verdict.candidate_id, str)
            or not isinstance(verdict.verdict, str)
            or verdict.verdict not in _VERDICTS
            or verdict.routable is not False
            for verdict in item.verdicts
        )
        for item in rows
    ):
        raise GmailTemporalFrontierError("candidate verdict rows are malformed")
    pages = {item.page_fingerprint: item for item in plan.pages}
    if len(pages) != len(plan.pages):
        raise GmailTemporalFrontierError("candidate page plan contains duplicates")
    row_page_ids = tuple(item.page_fingerprint for item in rows)
    if (
        len(row_page_ids) != len(set(row_page_ids))
        or set(row_page_ids) != set(pages)
        or any(
            item.routable is not False
            or item.frontier_fingerprint != plan.frontier_fingerprint
            or not isinstance(item.verdicts, tuple)
            or any(
                not isinstance(verdict, GmailTemporalCandidateVerdict)
                or not isinstance(verdict.candidate_id, str)
                for verdict in item.verdicts
            )
            for item in rows
        )
    ):
        raise GmailTemporalFrontierError(
            "candidate verdict rows do not cover the page plan exactly"
        )
    ordered_rows = tuple(
        sorted(rows, key=lambda item: pages[item.page_fingerprint].sequence)
    )
    seen_pages: set[str] = set()
    supported_ids: list[str] = []
    uncertain_ids: list[str] = []
    supported_citations: list[VerifiedGmailTemporalBatchCitation] = []
    uncertain_citations: list[VerifiedGmailTemporalBatchCitation] = []
    unsupported_count = 0
    accepted_by_binding: dict[str, str] = {}
    accepted_by_cluster: dict[str, str] = {}
    frontier = build_gmail_temporal_candidate_frontier(
        analysis=analysis,
        batch=batch,
    )
    candidates = {item.candidate_id: item for item in frontier.candidates}
    cluster_by_candidate = {
        candidate_id: cluster.cluster_id
        for page in plan.pages
        for cluster in page.clusters
        for candidate_id in cluster.candidate_ids
    }

    for row in ordered_rows:
        if (
            row.routable is not False
            or row.frontier_fingerprint != plan.frontier_fingerprint
            or row.page_fingerprint in seen_pages
            or row.page_fingerprint not in pages
            or not isinstance(row.verdicts, tuple)
        ):
            raise GmailTemporalFrontierError(
                "candidate verdict row is stale, duplicate, or malformed"
            )
        page = pages[row.page_fingerprint]
        _validate_page_integrity(frontier, page)
        expected_candidate_ids = tuple(
            candidate_id
            for cluster in page.clusters
            for candidate_id in cluster.candidate_ids
        )
        actual_candidate_ids = tuple(item.candidate_id for item in row.verdicts)
        if (
            len(actual_candidate_ids) != len(set(actual_candidate_ids))
            or set(actual_candidate_ids) != set(expected_candidate_ids)
        ):
            raise GmailTemporalFrontierError(
                "candidate verdict row does not cover its page exactly once"
            )
        seen_pages.add(row.page_fingerprint)
        verdicts = {item.candidate_id: item for item in row.verdicts}
        for candidate_id in expected_candidate_ids:
            verdict = verdicts[candidate_id]
            if verdict.routable is not False or verdict.verdict not in _VERDICTS:
                raise GmailTemporalFrontierError(
                    "candidate verdict is unsupported or routable"
                )
            if verdict.verdict == "unsupported":
                unsupported_count += 1
                continue
            candidate = candidates[candidate_id]
            existing = accepted_by_binding.get(candidate.binding_id)
            if existing is not None:
                raise GmailTemporalFrontierError(
                    "multiple candidate variants accepted for one binding"
                )
            accepted_by_binding[candidate.binding_id] = candidate_id
            cluster_id = cluster_by_candidate[candidate_id]
            if cluster_id in accepted_by_cluster:
                raise GmailTemporalFrontierError(
                    "multiple candidate variants accepted for one alias cluster"
                )
            accepted_by_cluster[cluster_id] = candidate_id
            citation = validate_gmail_temporal_candidate_page_choice(
                analysis=analysis,
                batch=batch,
                page=page,
                frontier_fingerprint=row.frontier_fingerprint,
                page_fingerprint=row.page_fingerprint,
                candidate_id=candidate_id,
            )
            if verdict.verdict == "supported":
                supported_ids.append(candidate_id)
                supported_citations.append(citation)
            else:
                uncertain_ids.append(candidate_id)
                uncertain_citations.append(citation)

    if seen_pages != set(pages):
        raise GmailTemporalFrontierError(
            "candidate verdict set omits one or more planned pages"
        )
    return GmailTemporalCandidateVerdictSet(
        version="gmail_temporal_candidate_verdict_set_v1",
        plan_fingerprint=plan.plan_fingerprint,
        frontier_fingerprint=plan.frontier_fingerprint,
        supported_candidate_ids=tuple(supported_ids),
        uncertain_candidate_ids=tuple(uncertain_ids),
        unsupported_candidate_count=unsupported_count,
        supported_citations=tuple(supported_citations),
        uncertain_citations=tuple(uncertain_citations),
        page_count=len(plan.pages),
        complete=plan.complete,
        requires_defer=(
            not plan.complete
            or not plan.covered_candidate_ids
            or bool(uncertain_ids)
            or any(candidates[value].requires_defer for value in supported_ids)
        ),
    )


def _candidate(
    *,
    analysis: TemporalLeadAnalysis,
    batch: GmailTemporalSelectorBatch,
    expression_id: str,
    subject_mention_id: str,
    lifecycle_mention_id: str | None,
) -> GmailTemporalVerificationCandidate | None:
    lead_id = next(
        (
            item.lead_id
            for item in batch.lead_hints
            if item.expression_id == expression_id
            and item.mention_id == subject_mention_id
        ),
        None,
    )
    try:
        citation = validate_gmail_temporal_batch_citation(
            batch,
            batch_fingerprint=batch.manifest.batch_fingerprint,
            expression_id=expression_id,
            subject_mention_id=subject_mention_id,
            lifecycle_mention_id=lifecycle_mention_id,
            selected_lead_id=lead_id,
        )
        selection = validate_gmail_temporal_selection(
            analysis,
            {
                "analysis_fingerprint": analysis.snapshot_fingerprint,
                "decision": "select_for_review",
                "associations": [
                    {
                        "expression_id": citation.expression_id,
                        "subject_mention_id": citation.subject_mention_id,
                        "lifecycle_mention_id": citation.lifecycle_mention_id,
                        "selected_lead_id": citation.selected_lead_id,
                    }
                ],
            },
        )
    except (GmailTemporalBatchAuthorityError, GmailTemporalSelectionError):
        return None
    if len(selection.associations) != 1:
        return None
    derived = selection.associations[0]
    if derived.lifecycle_mention_id != lifecycle_mention_id:
        return None
    candidate_id = _candidate_id(batch, citation, derived)
    return GmailTemporalVerificationCandidate(
        version="gmail_temporal_verification_candidate_v1",
        candidate_id=candidate_id,
        binding_id=_binding_id(batch, citation),
        expression_id=citation.expression_id,
        subject_mention_id=citation.subject_mention_id,
        lifecycle_mention_id=citation.lifecycle_mention_id,
        selected_lead_id=citation.selected_lead_id,
        relation=derived.relation,
        kind=derived.kind,
        lifecycle=derived.lifecycle,
        normalized_value=derived.normalized_value,
        blockers=derived.blockers,
        risk_features=derived.risk_features,
        repair_flags=derived.repair_flags,
        requires_defer=selection.decision == "defer_ambiguous",
        supporting_lead_present=derived.selected_lead_id is not None,
    )


def _candidate_id(
    batch: GmailTemporalSelectorBatch,
    citation: VerifiedGmailTemporalBatchCitation,
    derived: SelectedTemporalAssociation,
) -> str:
    material = {
        "version": _CANDIDATE_VERSION,
        "batch_fingerprint": batch.manifest.batch_fingerprint,
        "expression_id": citation.expression_id,
        "subject_mention_id": citation.subject_mention_id,
        "lifecycle_mention_id": citation.lifecycle_mention_id,
        "selected_lead_id": citation.selected_lead_id,
        "relation": derived.relation,
        "kind": derived.kind,
        "lifecycle": derived.lifecycle,
        "normalized_value": derived.normalized_value,
        "blockers": derived.blockers,
        "risk_features": derived.risk_features,
        "repair_flags": derived.repair_flags,
    }
    return "gtvc_" + hashlib.sha256(
        json.dumps(
            material,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()[:32]


def _binding_id(
    batch: GmailTemporalSelectorBatch,
    citation: VerifiedGmailTemporalBatchCitation,
) -> str:
    material = "\0".join(
        (
            batch.manifest.batch_fingerprint,
            citation.expression_id,
            citation.subject_mention_id,
        )
    ).encode("utf-8")
    return "gtvb_" + hashlib.sha256(material).hexdigest()[:32]


def _candidate_count_fragments(
    cluster: GmailTemporalCandidateCluster,
    *,
    max_candidates: int,
) -> tuple[GmailTemporalCandidateCluster, ...]:
    return tuple(
        _cluster_fragment(
            cluster,
            cluster.candidate_ids[start : start + max_candidates],
        )
        for start in range(0, len(cluster.candidate_ids), max_candidates)
    )


def _cluster_fragment(
    cluster: GmailTemporalCandidateCluster,
    candidate_ids: tuple[str, ...],
) -> GmailTemporalCandidateCluster:
    if not candidate_ids:
        raise GmailTemporalFrontierError("candidate cluster fragment cannot be empty")
    return GmailTemporalCandidateCluster(
        cluster_id=cluster.cluster_id,
        decision_unit_id=_decision_unit_id(
            cluster.cluster_id,
            candidate_ids,
        ),
        expression_id=cluster.expression_id,
        subject_mention_ids=cluster.subject_mention_ids,
        candidate_ids=candidate_ids,
    )


def _make_page(
    frontier: GmailTemporalCandidateFrontier,
    clusters: tuple[GmailTemporalCandidateCluster, ...],
    *,
    sequence: int,
) -> GmailTemporalCandidatePage:
    material = {
        "version": _PAGE_VERSION,
        "sequence": sequence,
        "frontier_fingerprint": frontier.frontier_fingerprint,
        "clusters": [asdict(item) for item in clusters],
    }
    return GmailTemporalCandidatePage(
        version="gmail_temporal_candidate_page_v1",
        sequence=sequence,
        page_fingerprint="gtfp_"
        + hashlib.sha256(_canonical_bytes(material)).hexdigest(),
        frontier_fingerprint=frontier.frontier_fingerprint,
        clusters=clusters,
    )


def _page_payload_bytes(
    frontier: GmailTemporalCandidateFrontier,
    page: GmailTemporalCandidatePage,
) -> bytes:
    candidates = {item.candidate_id: item for item in frontier.candidates}
    candidate_ids = tuple(
        candidate_id
        for cluster in page.clusters
        for candidate_id in cluster.candidate_ids
    )
    return _canonical_bytes(
        {
            "version": page.version,
            "sequence": page.sequence,
            "page_fingerprint": page.page_fingerprint,
            "frontier_fingerprint": page.frontier_fingerprint,
            "clusters": [asdict(item) for item in page.clusters],
            "candidates": [asdict(candidates[value]) for value in candidate_ids],
            "routable": False,
        }
    )


def _validate_page_integrity(
    frontier: GmailTemporalCandidateFrontier,
    page: GmailTemporalCandidatePage,
) -> None:
    if (
        not isinstance(frontier, GmailTemporalCandidateFrontier)
        or frontier.version != _FRONTIER_VERSION
        or frontier.routable is not False
        or not isinstance(page, GmailTemporalCandidatePage)
        or page.version != _PAGE_VERSION
        or isinstance(page.sequence, bool)
        or not isinstance(page.sequence, int)
        or page.sequence < 1
        or not isinstance(page.clusters, tuple)
        or not page.clusters
        or page.routable is not False
    ):
        raise GmailTemporalFrontierError("candidate page structure is invalid")
    if page.frontier_fingerprint != frontier.frontier_fingerprint:
        raise GmailTemporalFrontierError("candidate page is bound to another frontier")
    material = {
        "version": page.version,
        "sequence": page.sequence,
        "frontier_fingerprint": page.frontier_fingerprint,
        "clusters": [asdict(item) for item in page.clusters],
    }
    expected_fingerprint = "gtfp_" + hashlib.sha256(
        _canonical_bytes(material)
    ).hexdigest()
    if page.page_fingerprint != expected_fingerprint:
        raise GmailTemporalFrontierError("candidate page fingerprint is stale")

    candidates = {item.candidate_id: item for item in frontier.candidates}
    if len(candidates) != len(frontier.candidates):
        raise GmailTemporalFrontierError("candidate frontier contains duplicate IDs")
    seen: set[str] = set()
    for cluster in page.clusters:
        if (
            not isinstance(cluster, GmailTemporalCandidateCluster)
            or not isinstance(cluster.cluster_id, str)
            or not cluster.cluster_id
            or cluster.decision_unit_id
            != _decision_unit_id(cluster.cluster_id, cluster.candidate_ids)
            or not isinstance(cluster.expression_id, str)
            or not cluster.expression_id
            or not isinstance(cluster.subject_mention_ids, tuple)
            or not cluster.subject_mention_ids
            or len(cluster.subject_mention_ids)
            != len(set(cluster.subject_mention_ids))
            or not isinstance(cluster.candidate_ids, tuple)
            or not cluster.candidate_ids
            or len(cluster.candidate_ids) != len(set(cluster.candidate_ids))
            or cluster.routable is not False
        ):
            raise GmailTemporalFrontierError("candidate cluster structure is invalid")
        for candidate_id in cluster.candidate_ids:
            candidate = candidates.get(candidate_id)
            if (
                candidate is None
                or candidate.expression_id != cluster.expression_id
                or candidate.subject_mention_id not in cluster.subject_mention_ids
                or candidate_id in seen
            ):
                raise GmailTemporalFrontierError(
                    "candidate cluster exceeds its frontier authority"
                )
            seen.add(candidate_id)


def _validate_page_bounds(
    frontier: GmailTemporalCandidateFrontier,
    page: GmailTemporalCandidatePage,
    *,
    max_clusters: int,
    max_candidates: int,
    max_payload_bytes: int,
) -> None:
    _validate_page_integrity(frontier, page)
    if (
        len(page.clusters) > max_clusters
        or sum(len(item.candidate_ids) for item in page.clusters) > max_candidates
        or len(_page_payload_bytes(frontier, page)) > max_payload_bytes
    ):
        raise GmailTemporalFrontierError("candidate page exceeds its hard bounds")


def _alias_clusters(
    analysis: TemporalLeadAnalysis,
    batch: GmailTemporalSelectorBatch,
    frontier: GmailTemporalCandidateFrontier,
) -> tuple[GmailTemporalCandidateCluster, ...]:
    candidates_by_binding: dict[
        str, list[GmailTemporalVerificationCandidate]
    ] = {}
    for candidate in frontier.candidates:
        candidates_by_binding.setdefault(candidate.binding_id, []).append(candidate)
    if not candidates_by_binding:
        return ()
    subject_by_binding = {
        binding_id: values[0].subject_mention_id
        for binding_id, values in candidates_by_binding.items()
    }
    expression_by_binding = {
        binding_id: values[0].expression_id
        for binding_id, values in candidates_by_binding.items()
    }
    mentions = {item.mention_id: item for item in analysis.mentions}
    parent = {binding_id: binding_id for binding_id in candidates_by_binding}

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

    binding_ids = tuple(candidates_by_binding)
    for index, first_id in enumerate(binding_ids):
        first = mentions[subject_by_binding[first_id]]
        for second_id in binding_ids[index + 1 :]:
            if expression_by_binding[first_id] != expression_by_binding[second_id]:
                continue
            second = mentions[subject_by_binding[second_id]]
            types = {first.mention_type, second.mention_type}
            aliases = (
                first.field == second.field
                and "event_title_candidate" in types
                and bool(types & {"event", "event_predicate"})
                and first.start < second.end
                and second.start < first.end
            )
            if aliases:
                union(first_id, second_id)

    components: dict[str, list[str]] = {}
    for binding_id in binding_ids:
        components.setdefault(find(binding_id), []).append(binding_id)
    mention_rank = {
        item.mention_id: index for index, item in enumerate(batch.mentions)
    }
    output: list[GmailTemporalCandidateCluster] = []
    for component in components.values():
        ordered_bindings = tuple(
            sorted(
                component,
                key=lambda item: (
                    mention_rank.get(subject_by_binding[item], 1_000_000),
                    subject_by_binding[item],
                ),
            )
        )
        subject_ids = tuple(subject_by_binding[item] for item in ordered_bindings)
        candidate_ids = tuple(
            candidate.candidate_id
            for binding_id in ordered_bindings
            for candidate in candidates_by_binding[binding_id]
        )
        cluster_material = "\0".join(
            (
                frontier.frontier_fingerprint,
                expression_by_binding[ordered_bindings[0]],
                *subject_ids,
            )
        ).encode("utf-8")
        cluster_id = "gtfc_" + hashlib.sha256(cluster_material).hexdigest()[:32]
        output.append(
            GmailTemporalCandidateCluster(
                cluster_id=cluster_id,
                decision_unit_id=_decision_unit_id(
                    cluster_id,
                    candidate_ids,
                ),
                expression_id=expression_by_binding[ordered_bindings[0]],
                subject_mention_ids=subject_ids,
                candidate_ids=candidate_ids,
            )
        )
    return tuple(
        sorted(
            output,
            key=lambda item: (
                min(mention_rank.get(value, 1_000_000) for value in item.subject_mention_ids),
                item.expression_id,
                item.cluster_id,
            ),
        )
    )


def _omitted_candidate_mention_count(
    analysis: TemporalLeadAnalysis,
    batch: GmailTemporalSelectorBatch,
) -> int:
    selected = set(batch.manifest.mention_ids)
    relevant: set[str] = set()
    for mention in analysis.mentions:
        candidate_endpoint = (
            mention.mention_type in GMAIL_TEMPORAL_SUBJECT_TYPES
            or mention.mention_type == "lifecycle"
        )
        if not candidate_endpoint:
            continue
        local = (
            mention.field == batch.field
            and batch.segment_start <= mention.start
            and mention.end <= batch.segment_end
        )
        subject_bridge = batch.field == "body" and mention.field == "subject"
        if local or subject_bridge:
            relevant.add(mention.mention_id)
    return len(relevant - selected)


def _decision_unit_id(
    cluster_id: str,
    candidate_ids: tuple[str, ...],
) -> str:
    material = "\0".join((cluster_id, *candidate_ids)).encode("utf-8")
    return "gtfdu_" + hashlib.sha256(material).hexdigest()[:32]


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
