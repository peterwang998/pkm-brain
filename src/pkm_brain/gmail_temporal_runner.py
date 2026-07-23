from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Mapping

from .chunking import strip_frontmatter
from .db import connection
from .extraction_source_policy import source_extraction_admission
from .gmail_projection import (
    GMAIL_KNOWLEDGE_CLASSIFIER_VERSION,
    GMAIL_KNOWLEDGE_PROJECTION_VERSION,
)
from .gmail_sensitive_data import (
    GMAIL_SENSITIVE_DATA_VERSION,
    sanitize_gmail_model_payload,
)
from .gmail_temporal_batching import (
    GmailTemporalBatchPlan,
    GmailTemporalSelectorBatch,
    gmail_temporal_selector_batch_payload,
    plan_gmail_temporal_selector_batches,
)
from .gmail_temporal_frontier import (
    GmailTemporalCandidateFrontier,
    GmailTemporalCandidatePage,
    GmailTemporalCandidatePagePlan,
    GmailTemporalCandidatePageVerdicts,
    GmailTemporalCandidateVerdict,
    GmailTemporalVerificationCandidate,
    build_gmail_temporal_candidate_frontier,
    gmail_temporal_candidate_ensemble_policy_fingerprint,
    gmail_temporal_candidate_page_payload,
    plan_gmail_temporal_candidate_pages,
    validate_gmail_temporal_candidate_ensemble_verdict_set,
)
from .gmail_temporal_leads import (
    TemporalLeadAnalysis,
    analyze_gmail_temporal_leads,
)
from .gmail_temporal_persistence import (
    GmailTemporalPersistenceResult,
    GmailTemporalReviewComponentEvidence,
    GmailTemporalReviewExecutionEvidence,
    GmailTemporalSourceLocator,
    get_gmail_temporal_review_head,
    gmail_temporal_message_scope_key,
    persist_gmail_temporal_review_projection,
    persist_gmail_temporal_zero_work_outcome,
)
from .gmail_temporal_review import (
    GmailTemporalReviewBatchResult,
    gmail_temporal_review_grouping_policy_fingerprint,
    project_gmail_temporal_review,
)
from .gmail_temporal_selection import GMAIL_TEMPORAL_SUBJECT_TYPES
from .gmail_temporal_verifier import (
    GMAIL_TEMPORAL_CANDIDATE_VERIFIER_CONTRACT,
    GMAIL_TEMPORAL_VERDICTS,
    GMAIL_TEMPORAL_VERIFIER_MODEL,
    GMAIL_TEMPORAL_VERIFIER_POLICY_VERSION,
    GMAIL_TEMPORAL_VERIFIER_REASONING_EFFORT,
    gmail_temporal_verifier_policy_fingerprint,
)
from .paths import BrainPaths
from .source_dates import (
    gmail_message_source_evidence,
    source_frontmatter_with_path,
    strict_int,
    trusted_gmail_message_policies,
    trusted_gmail_message_timestamps,
)


GMAIL_TEMPORAL_RUNNER_VERSION = "gmail_temporal_review_runner_v2"
GMAIL_TEMPORAL_ADMISSION_POLICY_VERSION = "gmail_temporal_admission_policy_v2"
GMAIL_TEMPORAL_REQUEST_VERSION = "gmail_temporal_verifier_request_v1"
GMAIL_TEMPORAL_COMPONENT_VERSION = "gmail_temporal_verifier_component_v1"
GMAIL_TEMPORAL_PIPELINE_SCOPE = "gmail_temporal_review_v1"
GMAIL_TEMPORAL_EXTERNAL_PROVIDER = "external-codex"
GMAIL_TEMPORAL_MAX_COMPONENT_BYTES = 16 * 1024 * 1024

_RUN_COUNT = 3
_INVOCATION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_DISPOSITIONS = {
    "complete_review_projection",
    "no_recognized_expression",
    "no_verification_candidate",
    "not_admitted",
}
_COMPONENT_KEYS = {
    "version",
    "run_ordinal",
    "invocation_id",
    "provider",
    "model",
    "reasoning_effort",
    "started_at",
    "completed_at",
    "runner_policy_fingerprint",
    "admission_policy_fingerprint",
    "verifier_policy_fingerprint",
    "source_sha256",
    "analysis_fingerprint",
    "batch_plan_fingerprint",
    "target_fingerprint",
    "pages",
    "complete",
    "routable",
}
_COMPONENT_PAGE_KEYS = {
    "request_fingerprint",
    "batch_fingerprint",
    "frontier_fingerprint",
    "page_plan_fingerprint",
    "page_fingerprint",
    "verdicts",
}
_VERDICT_KEYS = {"candidate_id", "verdict"}


class GmailTemporalRunnerError(ValueError):
    """Raised when runtime temporal authority is incomplete or stale."""


@dataclass(frozen=True)
class GmailTemporalVerifierRequest:
    """One sanitized private request for one deterministic candidate page."""

    request_fingerprint: str
    batch_fingerprint: str
    frontier_fingerprint: str
    page_plan_fingerprint: str
    page_fingerprint: str
    candidate_count: int
    payload: str = field(repr=False)


@dataclass(frozen=True)
class GmailTemporalReviewPreparation:
    """Source-derived verifier work; payloads are private and must not be logged."""

    version: Literal["gmail_temporal_review_runner_v2"]
    disposition: str
    message_scope_key: str
    admission_basis: str
    runner_policy_fingerprint: str
    admission_policy_fingerprint: str
    verifier_policy_fingerprint: str
    source_sha256: str
    analysis_fingerprint: str
    batch_plan_fingerprint: str
    target_fingerprint: str
    expression_count: int
    batch_count: int
    candidate_count: int
    page_count: int
    requests: tuple[GmailTemporalVerifierRequest, ...]
    private_content: Literal[True] = True
    routable: Literal[False] = False


@dataclass(frozen=True)
class GmailTemporalRunnerResult:
    """Aggregate-only finalizer result safe for operational status output."""

    version: Literal["gmail_temporal_review_runner_v2"]
    disposition: str
    message_scope_key: str
    admission_basis: str
    expression_count: int
    batch_count: int
    candidate_count: int
    page_count: int
    component_count: int
    artifact_count: int
    cluster_review_count: int
    group_count: int
    persisted: bool
    head_cleared: bool
    run_id: str | None
    head_generation: int | None
    execution_id: str
    replayed: bool
    head_changed: bool
    independent_invocations_verified: Literal[False] = False
    private_content_printed: Literal[False] = False
    routable: Literal[False] = False


@dataclass(frozen=True)
class _TrustedMessage:
    text: str
    locator: GmailTemporalSourceLocator
    message_scope_key: str
    frontmatter: Mapping[str, Any]
    message_policy: Mapping[str, Any]
    fact_source_admitted: bool


@dataclass(frozen=True)
class _BatchAuthority:
    batch: GmailTemporalSelectorBatch
    frontier_candidates: tuple[GmailTemporalVerificationCandidate, ...]
    page_plan: GmailTemporalCandidatePagePlan


@dataclass(frozen=True)
class _Authority:
    source: _TrustedMessage
    admission_basis: str
    analysis: TemporalLeadAnalysis
    batch_plan: GmailTemporalBatchPlan
    batches: tuple[_BatchAuthority, ...]
    disposition: str
    target_fingerprint: str
    requests: tuple[GmailTemporalVerifierRequest, ...]


@dataclass(frozen=True)
class _Component:
    fingerprint: str
    run_ordinal: int
    invocation_id: str
    started_at: str
    completed_at: str
    payload_json: str = field(repr=False)
    rows_by_batch: Mapping[str, tuple[GmailTemporalCandidatePageVerdicts, ...]]


def gmail_temporal_admission_policy_fingerprint() -> str:
    material = {
        "version": GMAIL_TEMPORAL_ADMISSION_POLICY_VERSION,
        "projection_version": GMAIL_KNOWLEDGE_PROJECTION_VERSION,
        "classifier_version": GMAIL_KNOWLEDGE_CLASSIFIER_VERSION,
        "fact_admission": ("source_extraction_admission_and_target_message_fact_basis"),
        "hard_suppression": [
            "document_deleted",
            "target_provider_promotion_unless_starred",
        ],
        "weak_advertising_evidence": ["content_pattern"],
        "bulk_rescue": {
            "requires_target_relevance_signal": True,
            "signals": [
                "provider_important",
                "provider_starred",
                "human_signal_basis",
                "operator_message_after",
            ],
        },
        "rescue_inventory": {
            "requires_expression": True,
            "mention_types": sorted(GMAIL_TEMPORAL_SUBJECT_TYPES),
            "important_temporal_is_not_recall_boundary": True,
        },
        "rescue_routable": False,
    }
    return "gtap_" + hashlib.sha256(_canonical_bytes(material)).hexdigest()


def gmail_temporal_runner_policy_fingerprint() -> str:
    material = {
        "version": GMAIL_TEMPORAL_RUNNER_VERSION,
        "pipeline_scope": GMAIL_TEMPORAL_PIPELINE_SCOPE,
        "admission_policy": gmail_temporal_admission_policy_fingerprint(),
        "sanitizer_version": GMAIL_SENSITIVE_DATA_VERSION,
        "provider": GMAIL_TEMPORAL_EXTERNAL_PROVIDER,
        "model": GMAIL_TEMPORAL_VERIFIER_MODEL,
        "reasoning_effort": GMAIL_TEMPORAL_VERIFIER_REASONING_EFFORT,
        "verifier_policy": gmail_temporal_verifier_policy_fingerprint(),
        "ensemble_policy": gmail_temporal_candidate_ensemble_policy_fingerprint(),
        "grouping_policy": gmail_temporal_review_grouping_policy_fingerprint(),
        "component_count": _RUN_COUNT,
        "source_and_intermediates_are_recomputed": True,
        "zero_work_fabricates_component_evidence": False,
    }
    return "gtrun_" + hashlib.sha256(_canonical_bytes(material)).hexdigest()


def prepare_gmail_temporal_review(
    paths: BrainPaths,
    *,
    document_id: str,
    gmail_message_id: str,
) -> GmailTemporalReviewPreparation:
    """Reconstruct trusted source and return sanitized external-verifier work.

    The returned request payloads contain private Gmail evidence. Callers must
    keep them in the restricted Gmail external-Codex boundary and must not log
    or persist them in the main Brain database.
    """

    authority = _build_authority(
        paths,
        document_id=document_id,
        gmail_message_id=gmail_message_id,
    )
    return GmailTemporalReviewPreparation(
        version=GMAIL_TEMPORAL_RUNNER_VERSION,
        disposition=authority.disposition,
        message_scope_key=authority.source.message_scope_key,
        admission_basis=authority.admission_basis,
        runner_policy_fingerprint=gmail_temporal_runner_policy_fingerprint(),
        admission_policy_fingerprint=gmail_temporal_admission_policy_fingerprint(),
        verifier_policy_fingerprint=gmail_temporal_verifier_policy_fingerprint(),
        source_sha256=authority.source.locator.source_sha256,
        analysis_fingerprint=authority.analysis.snapshot_fingerprint,
        batch_plan_fingerprint=authority.batch_plan.plan_fingerprint,
        target_fingerprint=authority.target_fingerprint,
        expression_count=len(authority.analysis.expressions),
        batch_count=len(authority.batch_plan.batches),
        candidate_count=sum(
            len(item.frontier_candidates) for item in authority.batches
        ),
        page_count=sum(len(item.page_plan.pages) for item in authority.batches),
        requests=authority.requests,
    )


def run_gmail_temporal_review(
    paths: BrainPaths,
    *,
    document_id: str,
    gmail_message_id: str,
    component_artifacts: tuple[Path, ...],
) -> GmailTemporalRunnerResult:
    """Finalize exactly three external verdict runs through the private sink.

    Runtime callers cannot supply text, admission decisions, intermediate
    authority, model configuration, component hashes, projections, or pipeline
    scope. Every such value is reconstructed or pinned here.
    """

    authority = _build_authority(
        paths,
        document_id=document_id,
        gmail_message_id=gmail_message_id,
    )
    counts = _authority_counts(authority)
    if authority.disposition != "complete_review_projection":
        if component_artifacts:
            raise GmailTemporalRunnerError(
                "verifier artifacts are invalid for a zero-work disposition"
            )
        head = get_gmail_temporal_review_head(
            paths,
            message_scope_key=authority.source.message_scope_key,
            pipeline_scope=GMAIL_TEMPORAL_PIPELINE_SCOPE,
        )
        persisted = persist_gmail_temporal_zero_work_outcome(
            paths,
            source=authority.source.locator,
            pipeline_scope=GMAIL_TEMPORAL_PIPELINE_SCOPE,
            execution=_execution_evidence(authority, ()),
            expected_head_run_id=head.run_id if head is not None else None,
            expected_head_generation=head.generation if head is not None else None,
        )
        return GmailTemporalRunnerResult(
            version=GMAIL_TEMPORAL_RUNNER_VERSION,
            disposition=authority.disposition,
            message_scope_key=authority.source.message_scope_key,
            admission_basis=authority.admission_basis,
            **counts,
            component_count=0,
            artifact_count=0,
            cluster_review_count=0,
            group_count=0,
            persisted=True,
            head_cleared=persisted.head_changed,
            run_id=None,
            head_generation=persisted.head_generation,
            execution_id=persisted.execution_id,
            replayed=persisted.replayed,
            head_changed=persisted.head_changed,
        )

    components = _load_components(component_artifacts, authority=authority)
    batch_results: list[GmailTemporalReviewBatchResult] = []
    component_fingerprints = tuple(item.fingerprint for item in components)
    for item in authority.batches:
        runs = tuple(
            component.rows_by_batch[item.batch.manifest.batch_fingerprint]
            for component in components
        )
        ensemble = validate_gmail_temporal_candidate_ensemble_verdict_set(
            analysis=authority.analysis,
            batch=item.batch,
            plan=item.page_plan,
            runs=runs,
        )
        batch_results.append(
            GmailTemporalReviewBatchResult(
                batch=item.batch,
                page_plan=item.page_plan,
                ensemble=ensemble,
                component_evidence_fingerprints=component_fingerprints,
            )
        )
    projection = project_gmail_temporal_review(
        text=authority.source.text,
        analysis=authority.analysis,
        batch_plan=authority.batch_plan,
        batch_results=tuple(batch_results),
    )
    head = get_gmail_temporal_review_head(
        paths,
        message_scope_key=authority.source.message_scope_key,
        pipeline_scope=GMAIL_TEMPORAL_PIPELINE_SCOPE,
    )
    persistence = persist_gmail_temporal_review_projection(
        paths,
        source=authority.source.locator,
        pipeline_scope=GMAIL_TEMPORAL_PIPELINE_SCOPE,
        projection=projection,
        expected_head_run_id=head.run_id if head is not None else None,
        expected_head_generation=head.generation if head is not None else None,
        execution=_execution_evidence(authority, components),
    )
    return _completed_result(authority, projection, persistence)


def _build_authority(
    paths: BrainPaths,
    *,
    document_id: str,
    gmail_message_id: str,
) -> _Authority:
    source = _load_trusted_message(
        paths,
        document_id=document_id,
        gmail_message_id=gmail_message_id,
    )
    chunk_id = source.message_scope_key
    inventory = analyze_gmail_temporal_leads(
        text=source.text,
        message_internal_at=source.locator.message_internal_at,
        fact_admitted=False,
        temporal_review_rescue=False,
        chunk_id=chunk_id,
    )
    admission_basis = _admission_basis(source, gmail_message_id, inventory)
    if admission_basis == "fact":
        analysis = analyze_gmail_temporal_leads(
            text=source.text,
            message_internal_at=source.locator.message_internal_at,
            fact_admitted=True,
            temporal_review_rescue=False,
            chunk_id=chunk_id,
        )
    elif admission_basis == "temporal_rescue":
        analysis = analyze_gmail_temporal_leads(
            text=source.text,
            message_internal_at=source.locator.message_internal_at,
            fact_admitted=False,
            temporal_review_rescue=True,
            chunk_id=chunk_id,
        )
    else:
        analysis = inventory

    _validate_analysis_authority(analysis)
    batch_plan = plan_gmail_temporal_selector_batches(
        text=source.text, analysis=analysis
    )
    if batch_plan.omissions:
        if admission_basis == "not_admitted":
            return _zero_authority(
                source=source,
                admission_basis=admission_basis,
                analysis=analysis,
                batch_plan=batch_plan,
                disposition="not_admitted",
            )
        raise GmailTemporalRunnerError("temporal batch authority is incomplete")
    if not analysis.expressions:
        return _zero_authority(
            source=source,
            admission_basis=admission_basis,
            analysis=analysis,
            batch_plan=batch_plan,
            disposition=(
                "not_admitted"
                if admission_basis == "not_admitted"
                else "no_recognized_expression"
            ),
        )
    if admission_basis == "not_admitted":
        return _zero_authority(
            source=source,
            admission_basis=admission_basis,
            analysis=analysis,
            batch_plan=batch_plan,
            disposition="not_admitted",
        )

    batches: list[_BatchAuthority] = []
    requests: list[GmailTemporalVerifierRequest] = []
    seen_candidates: set[str] = set()
    for batch in batch_plan.batches:
        frontier = build_gmail_temporal_candidate_frontier(
            analysis=analysis,
            batch=batch,
        )
        if not frontier.complete or frontier.omitted_candidate_mention_count:
            raise GmailTemporalRunnerError("temporal candidate frontier is incomplete")
        candidate_ids = {item.candidate_id for item in frontier.candidates}
        if seen_candidates & candidate_ids:
            raise GmailTemporalRunnerError("temporal candidate authority is duplicated")
        seen_candidates.update(candidate_ids)
        page_plan = plan_gmail_temporal_candidate_pages(
            analysis=analysis,
            batch=batch,
        )
        if (
            not page_plan.complete
            or set(page_plan.covered_candidate_ids) != candidate_ids
        ):
            raise GmailTemporalRunnerError("temporal candidate page plan is incomplete")
        batches.append(
            _BatchAuthority(
                batch=batch,
                frontier_candidates=frontier.candidates,
                page_plan=page_plan,
            )
        )
        requests.extend(
            _request_for_page(
                batch=batch,
                frontier=frontier,
                page_plan=page_plan,
                page=page,
            )
            for page in page_plan.pages
        )
    if not seen_candidates:
        return _zero_authority(
            source=source,
            admission_basis=admission_basis,
            analysis=analysis,
            batch_plan=batch_plan,
            disposition="no_verification_candidate",
            batches=tuple(batches),
        )
    target_fingerprint = _target_fingerprint(
        source=source,
        admission_basis=admission_basis,
        analysis=analysis,
        batch_plan=batch_plan,
        requests=tuple(requests),
    )
    return _Authority(
        source=source,
        admission_basis=admission_basis,
        analysis=analysis,
        batch_plan=batch_plan,
        batches=tuple(batches),
        disposition="complete_review_projection",
        target_fingerprint=target_fingerprint,
        requests=tuple(requests),
    )


def _load_trusted_message(
    paths: BrainPaths,
    *,
    document_id: str,
    gmail_message_id: str,
) -> _TrustedMessage:
    if not isinstance(document_id, str) or not document_id.strip():
        raise GmailTemporalRunnerError("document identity is invalid")
    if not isinstance(gmail_message_id, str) or not gmail_message_id.strip():
        raise GmailTemporalRunnerError("message identity is invalid")
    with connection(paths.sqlite_path) as conn:
        row = conn.execute(
            "SELECT * FROM documents WHERE id = ? AND status = 'active'",
            (document_id,),
        ).fetchone()
    if row is None or row["source_type"] != "gmail_thread":
        raise GmailTemporalRunnerError("active Gmail source authority is unavailable")
    document = dict(row)
    frontmatter, source_path = source_frontmatter_with_path(document)
    if source_path is None or source_path.is_symlink():
        raise GmailTemporalRunnerError("trusted Gmail source file is unavailable")
    try:
        file_stat = source_path.stat()
    except OSError as exc:
        raise GmailTemporalRunnerError(
            "trusted Gmail source file is unavailable"
        ) from exc
    if not stat.S_ISREG(file_stat.st_mode):
        raise GmailTemporalRunnerError("trusted Gmail source file is unavailable")
    if (
        strict_int(frontmatter.get("gmail_projection_version"))
        != GMAIL_KNOWLEDGE_PROJECTION_VERSION
        or strict_int(frontmatter.get("gmail_classifier_version"))
        != GMAIL_KNOWLEDGE_CLASSIFIER_VERSION
    ):
        raise GmailTemporalRunnerError("Gmail source policy version is stale")
    timestamps = trusted_gmail_message_timestamps(document, frontmatter, source_path)
    if timestamps is None:
        raise GmailTemporalRunnerError("trusted Gmail message index is invalid")
    policies = trusted_gmail_message_policies(
        document,
        frontmatter,
        source_path,
    )
    if policies is None:
        raise GmailTemporalRunnerError("trusted Gmail message policy is invalid")
    matches = [
        item
        for item in timestamps
        if str(item.get("message_id") or "") == gmail_message_id
    ]
    if len(matches) != 1:
        raise GmailTemporalRunnerError("trusted Gmail message identity is unavailable")
    match = matches[0]
    policy_matches = [
        item
        for item in policies
        if str(item.get("message_id") or "") == gmail_message_id
    ]
    if len(policy_matches) != 1:
        raise GmailTemporalRunnerError("trusted Gmail message policy is unavailable")
    internal_at = str(match.get("internal_date") or "")
    if not _aware_timestamp(internal_at):
        raise GmailTemporalRunnerError("trusted Gmail assertion clock is unavailable")
    try:
        source_bytes = source_path.read_bytes()
        source_text = source_bytes.decode("utf-8")
    except (OSError, UnicodeError) as exc:
        raise GmailTemporalRunnerError(
            "trusted Gmail source could not be read"
        ) from exc
    content_hash = hashlib.sha256(source_bytes).hexdigest()
    if content_hash != str(document.get("content_hash") or ""):
        raise GmailTemporalRunnerError(
            "trusted Gmail source changed during preparation"
        )
    fact_source_admitted, _source_metadata = source_extraction_admission(
        document,
        {"require_fact_eligible": True},
    )
    body = strip_frontmatter(source_text)
    start = int(match["start_offset"])
    end = int(match["end_offset"])
    evidence = gmail_message_source_evidence(body[start:end])
    if evidence is None:
        raise GmailTemporalRunnerError("trusted Gmail selector input is invalid")
    source_sha256 = hashlib.sha256(evidence.text.encode("utf-8")).hexdigest()
    account_key = str(frontmatter.get("gmail_account_key") or "").strip()
    thread_id = str(frontmatter.get("gmail_thread_id") or "").strip()
    source_revision = str(frontmatter.get("gmail_source_revision") or "").strip()
    locator = GmailTemporalSourceLocator(
        document_id=document_id,
        document_content_hash=content_hash,
        gmail_account_key=account_key,
        gmail_thread_id=thread_id,
        gmail_source_revision=source_revision,
        gmail_message_id=gmail_message_id,
        message_internal_at=internal_at,
        message_start_offset=start,
        message_end_offset=end,
        source_sha256=source_sha256,
    ).validated()
    scope_key = gmail_temporal_message_scope_key(
        gmail_account_key=account_key,
        gmail_thread_id=thread_id,
        gmail_message_id=gmail_message_id,
    )
    return _TrustedMessage(
        text=evidence.text,
        locator=locator,
        message_scope_key=scope_key,
        frontmatter=dict(frontmatter),
        message_policy=dict(policy_matches[0]),
        fact_source_admitted=fact_source_admitted,
    )


def _admission_basis(
    source: _TrustedMessage,
    gmail_message_id: str,
    inventory: TemporalLeadAnalysis,
) -> str:
    frontmatter = source.frontmatter
    policy = source.message_policy
    if policy.get("message_id") != gmail_message_id:
        raise GmailTemporalRunnerError("Gmail target message policy is invalid")
    advertising_bases = set(policy.get("advertising_bases") or ())
    if frontmatter.get("deleted") is True or (
        "provider_category_promotions" in advertising_bases
        and policy.get("provider_starred") is not True
    ):
        return "not_admitted"
    if source.fact_source_admitted and policy.get("fact_admission_basis") != "none":
        return "fact"
    if policy.get("delivery_kind") == "bulk" and not (
        policy.get("provider_important") is True
        or policy.get("provider_starred") is True
        or policy.get("human_signal_basis") != "none"
        or policy.get("operator_message_after") is True
    ):
        return "not_admitted"
    has_subject = any(
        item.mention_type in GMAIL_TEMPORAL_SUBJECT_TYPES for item in inventory.mentions
    )
    return (
        "temporal_rescue" if inventory.expressions and has_subject else "not_admitted"
    )


def _validate_analysis_authority(analysis: TemporalLeadAnalysis) -> None:
    # Expressions and mentions remain complete even when the bounded ranking-hint
    # graph samples endpoints.  Hints can rank verifier authority but may never
    # define its recall boundary; batch/frontier coverage is checked separately.
    if not analysis.scope_bound:
        raise GmailTemporalRunnerError("temporal analysis authority is incomplete")


def _request_for_page(
    *,
    batch: GmailTemporalSelectorBatch,
    frontier: GmailTemporalCandidateFrontier,
    page_plan: GmailTemporalCandidatePagePlan,
    page: GmailTemporalCandidatePage,
) -> GmailTemporalVerifierRequest:
    batch_value = json.loads(gmail_temporal_selector_batch_payload(batch))
    page_value = json.loads(
        gmail_temporal_candidate_page_payload(frontier=frontier, page=page)
    )
    material = {
        "version": GMAIL_TEMPORAL_REQUEST_VERSION,
        "runner_policy_fingerprint": gmail_temporal_runner_policy_fingerprint(),
        "admission_policy_fingerprint": gmail_temporal_admission_policy_fingerprint(),
        "verifier_policy_version": GMAIL_TEMPORAL_VERIFIER_POLICY_VERSION,
        "verifier_policy_fingerprint": gmail_temporal_verifier_policy_fingerprint(),
        "contract": GMAIL_TEMPORAL_CANDIDATE_VERIFIER_CONTRACT,
        "response_schema": {
            "request_fingerprint": "echo_exactly",
            "verdicts": [
                {
                    "candidate_id": "echo_exactly",
                    "verdict": list(GMAIL_TEMPORAL_VERDICTS),
                }
            ],
        },
        "batch": batch_value,
        "page": page_value,
    }
    sanitized = sanitize_gmail_model_payload(material)
    payload_without_fingerprint = _canonical_bytes(sanitized)
    request_fingerprint = (
        "gtrq_" + hashlib.sha256(payload_without_fingerprint).hexdigest()
    )
    payload = _canonical_bytes(
        {**sanitized, "request_fingerprint": request_fingerprint}
    ).decode("utf-8")
    candidate_count = sum(len(item.candidate_ids) for item in page.clusters)
    return GmailTemporalVerifierRequest(
        request_fingerprint=request_fingerprint,
        batch_fingerprint=batch.manifest.batch_fingerprint,
        frontier_fingerprint=frontier.frontier_fingerprint,
        page_plan_fingerprint=page_plan.plan_fingerprint,
        page_fingerprint=page.page_fingerprint,
        candidate_count=candidate_count,
        payload=payload,
    )


def _target_fingerprint(
    *,
    source: _TrustedMessage,
    admission_basis: str,
    analysis: TemporalLeadAnalysis,
    batch_plan: GmailTemporalBatchPlan,
    requests: tuple[GmailTemporalVerifierRequest, ...],
) -> str:
    material = {
        "runner_policy_fingerprint": gmail_temporal_runner_policy_fingerprint(),
        "admission_policy_fingerprint": gmail_temporal_admission_policy_fingerprint(),
        "verifier_policy_fingerprint": gmail_temporal_verifier_policy_fingerprint(),
        "source_sha256": source.locator.source_sha256,
        "admission_basis": admission_basis,
        "target_message_policy": dict(source.message_policy),
        "analysis_fingerprint": analysis.snapshot_fingerprint,
        "batch_plan_fingerprint": batch_plan.plan_fingerprint,
        "request_fingerprints": [item.request_fingerprint for item in requests],
    }
    return "gtrt_" + hashlib.sha256(_canonical_bytes(material)).hexdigest()


def _zero_authority(
    *,
    source: _TrustedMessage,
    admission_basis: str,
    analysis: TemporalLeadAnalysis,
    batch_plan: GmailTemporalBatchPlan,
    disposition: str,
    batches: tuple[_BatchAuthority, ...] = (),
) -> _Authority:
    if disposition not in _DISPOSITIONS:
        raise GmailTemporalRunnerError("temporal runner disposition is invalid")
    target = _target_fingerprint(
        source=source,
        admission_basis=admission_basis,
        analysis=analysis,
        batch_plan=batch_plan,
        requests=(),
    )
    return _Authority(
        source=source,
        admission_basis=admission_basis,
        analysis=analysis,
        batch_plan=batch_plan,
        batches=batches,
        disposition=disposition,
        target_fingerprint=target,
        requests=(),
    )


def _load_components(
    paths: tuple[Path, ...],
    *,
    authority: _Authority,
) -> tuple[_Component, ...]:
    if not isinstance(paths, tuple) or len(paths) != _RUN_COUNT:
        raise GmailTemporalRunnerError("exactly three verifier artifacts are required")
    resolved = [Path(path).resolve() for path in paths]
    if len(set(resolved)) != _RUN_COUNT:
        raise GmailTemporalRunnerError("verifier artifacts must use distinct paths")
    expected_requests = {item.page_fingerprint: item for item in authority.requests}
    page_authority: dict[str, tuple[_BatchAuthority, GmailTemporalCandidatePage]] = {}
    for batch in authority.batches:
        for page in batch.page_plan.pages:
            page_authority[page.page_fingerprint] = (batch, page)
    components: list[_Component] = []
    inodes: set[tuple[int, int]] = set()
    invocations: set[str] = set()
    for expected_ordinal, path in enumerate(paths, start=1):
        raw, file_stat = _read_private_component(Path(path))
        inode = (file_stat.st_dev, file_stat.st_ino)
        if inode in inodes:
            raise GmailTemporalRunnerError("verifier artifacts must be distinct files")
        inodes.add(inode)
        value = _strict_json(raw)
        if not isinstance(value, Mapping) or set(value) != _COMPONENT_KEYS:
            raise GmailTemporalRunnerError("verifier artifact schema is invalid")
        invocation = value.get("invocation_id")
        run_ordinal = value.get("run_ordinal")
        if (
            value.get("version") != GMAIL_TEMPORAL_COMPONENT_VERSION
            or not isinstance(run_ordinal, int)
            or isinstance(run_ordinal, bool)
            or run_ordinal != expected_ordinal
            or not isinstance(invocation, str)
            or _INVOCATION_ID_RE.fullmatch(invocation) is None
            or invocation in invocations
            or value.get("provider") != GMAIL_TEMPORAL_EXTERNAL_PROVIDER
            or value.get("model") != GMAIL_TEMPORAL_VERIFIER_MODEL
            or value.get("reasoning_effort") != GMAIL_TEMPORAL_VERIFIER_REASONING_EFFORT
            or value.get("runner_policy_fingerprint")
            != gmail_temporal_runner_policy_fingerprint()
            or value.get("admission_policy_fingerprint")
            != gmail_temporal_admission_policy_fingerprint()
            or value.get("verifier_policy_fingerprint")
            != gmail_temporal_verifier_policy_fingerprint()
            or value.get("source_sha256") != authority.source.locator.source_sha256
            or value.get("analysis_fingerprint")
            != authority.analysis.snapshot_fingerprint
            or value.get("batch_plan_fingerprint")
            != authority.batch_plan.plan_fingerprint
            or value.get("target_fingerprint") != authority.target_fingerprint
            or value.get("complete") is not True
            or value.get("routable") is not False
        ):
            raise GmailTemporalRunnerError("verifier artifact authority is invalid")
        invocations.add(invocation)
        started = _parse_timestamp(value.get("started_at"))
        completed = _parse_timestamp(value.get("completed_at"))
        if started is None or completed is None or completed < started:
            raise GmailTemporalRunnerError("verifier artifact chronology is invalid")
        raw_pages = value.get("pages")
        if not isinstance(raw_pages, list):
            raise GmailTemporalRunnerError("verifier artifact pages are invalid")
        rows_by_batch: dict[str, list[GmailTemporalCandidatePageVerdicts]] = {
            item.batch.manifest.batch_fingerprint: [] for item in authority.batches
        }
        seen_pages: set[str] = set()
        for raw_page in raw_pages:
            if (
                not isinstance(raw_page, Mapping)
                or set(raw_page) != _COMPONENT_PAGE_KEYS
            ):
                raise GmailTemporalRunnerError("verifier page schema is invalid")
            page_fingerprint = raw_page.get("page_fingerprint")
            if (
                not isinstance(page_fingerprint, str)
                or page_fingerprint in seen_pages
                or page_fingerprint not in expected_requests
            ):
                raise GmailTemporalRunnerError("verifier page authority is invalid")
            seen_pages.add(page_fingerprint)
            expected = expected_requests[page_fingerprint]
            batch_authority, page = page_authority[page_fingerprint]
            if (
                raw_page.get("request_fingerprint") != expected.request_fingerprint
                or raw_page.get("batch_fingerprint") != expected.batch_fingerprint
                or raw_page.get("frontier_fingerprint") != expected.frontier_fingerprint
                or raw_page.get("page_plan_fingerprint")
                != expected.page_plan_fingerprint
            ):
                raise GmailTemporalRunnerError("verifier page fingerprints are stale")
            verdicts = _parse_verdicts(raw_page.get("verdicts"), page=page)
            rows_by_batch[batch_authority.batch.manifest.batch_fingerprint].append(
                GmailTemporalCandidatePageVerdicts(
                    frontier_fingerprint=expected.frontier_fingerprint,
                    page_fingerprint=page_fingerprint,
                    verdicts=verdicts,
                )
            )
        if seen_pages != set(expected_requests):
            raise GmailTemporalRunnerError(
                "verifier artifact page coverage is incomplete"
            )
        canonical = _canonical_bytes(value) + b"\n"
        if raw != canonical:
            raise GmailTemporalRunnerError("verifier artifact is not canonical JSON")
        components.append(
            _Component(
                fingerprint=hashlib.sha256(raw).hexdigest(),
                run_ordinal=run_ordinal,
                invocation_id=invocation,
                started_at=str(value["started_at"]),
                completed_at=str(value["completed_at"]),
                payload_json=raw.decode("utf-8"),
                rows_by_batch={
                    key: tuple(value) for key, value in rows_by_batch.items()
                },
            )
        )
    return tuple(components)


def _parse_verdicts(
    value: Any,
    *,
    page: GmailTemporalCandidatePage,
) -> tuple[GmailTemporalCandidateVerdict, ...]:
    if not isinstance(value, list):
        raise GmailTemporalRunnerError("verifier candidate verdicts are invalid")
    expected = tuple(
        candidate_id
        for cluster in page.clusters
        for candidate_id in cluster.candidate_ids
    )
    parsed: list[GmailTemporalCandidateVerdict] = []
    for item in value:
        if not isinstance(item, Mapping) or set(item) != _VERDICT_KEYS:
            raise GmailTemporalRunnerError(
                "verifier candidate verdict schema is invalid"
            )
        candidate_id = item.get("candidate_id")
        verdict = item.get("verdict")
        if not isinstance(candidate_id, str) or verdict not in GMAIL_TEMPORAL_VERDICTS:
            raise GmailTemporalRunnerError("verifier candidate verdict is invalid")
        parsed.append(GmailTemporalCandidateVerdict(candidate_id, verdict))
    actual = tuple(item.candidate_id for item in parsed)
    if len(actual) != len(set(actual)) or set(actual) != set(expected):
        raise GmailTemporalRunnerError("verifier candidate coverage is incomplete")
    return tuple(parsed)


def _read_private_component(path: Path) -> tuple[bytes, os.stat_result]:
    descriptor: int | None = None
    try:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        if nofollow:
            flags |= nofollow
        elif path.is_symlink():
            raise GmailTemporalRunnerError("verifier artifact is not a protected file")
        descriptor = os.open(path, flags)
        initial = os.fstat(descriptor)
        if (
            not stat.S_ISREG(initial.st_mode)
            or stat.S_IMODE(initial.st_mode) != 0o600
            or initial.st_uid != os.geteuid()
            or initial.st_nlink != 1
        ):
            raise GmailTemporalRunnerError("verifier artifact is not a protected file")
        if initial.st_size <= 0 or initial.st_size > GMAIL_TEMPORAL_MAX_COMPONENT_BYTES:
            raise GmailTemporalRunnerError("verifier artifact size is invalid")
        chunks: list[bytes] = []
        remaining = initial.st_size
        while remaining:
            chunk = os.read(descriptor, min(remaining, 1024 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        final = os.fstat(descriptor)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise GmailTemporalRunnerError(
                "verifier artifact is not a protected file"
            ) from exc
        raise GmailTemporalRunnerError("verifier artifact could not be read") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if (
        initial.st_dev,
        initial.st_ino,
        initial.st_size,
        initial.st_mtime_ns,
    ) != (final.st_dev, final.st_ino, final.st_size, final.st_mtime_ns):
        raise GmailTemporalRunnerError("verifier artifact changed during validation")
    if len(raw) != initial.st_size:
        raise GmailTemporalRunnerError("verifier artifact changed during validation")
    return raw, final


def _strict_json(raw: bytes) -> Any:
    def object_pairs(values: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in values:
            if key in result:
                raise GmailTemporalRunnerError("verifier artifact has duplicate keys")
            result[key] = value
        return result

    try:
        return json.loads(raw, object_pairs_hook=object_pairs)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise GmailTemporalRunnerError("verifier artifact is not valid JSON") from exc


def _execution_evidence(
    authority: _Authority,
    components: tuple[_Component, ...],
) -> GmailTemporalReviewExecutionEvidence:
    counts = _authority_counts(authority)
    return GmailTemporalReviewExecutionEvidence(
        runner_policy_fingerprint=gmail_temporal_runner_policy_fingerprint(),
        admission_policy_fingerprint=gmail_temporal_admission_policy_fingerprint(),
        verifier_policy_fingerprint=gmail_temporal_verifier_policy_fingerprint(),
        sanitizer_version=GMAIL_SENSITIVE_DATA_VERSION,
        provider=GMAIL_TEMPORAL_EXTERNAL_PROVIDER,
        model=GMAIL_TEMPORAL_VERIFIER_MODEL,
        reasoning_effort=GMAIL_TEMPORAL_VERIFIER_REASONING_EFFORT,
        admission_basis=authority.admission_basis,
        disposition=authority.disposition,
        target_fingerprint=authority.target_fingerprint,
        analysis_fingerprint=authority.analysis.snapshot_fingerprint,
        batch_plan_fingerprint=authority.batch_plan.plan_fingerprint,
        expression_count=counts["expression_count"],
        batch_count=counts["batch_count"],
        candidate_count=counts["candidate_count"],
        page_count=counts["page_count"],
        request_fingerprints=tuple(
            item.request_fingerprint for item in authority.requests
        ),
        components=tuple(
            GmailTemporalReviewComponentEvidence(
                run_ordinal=item.run_ordinal,
                invocation_id=item.invocation_id,
                started_at=item.started_at,
                completed_at=item.completed_at,
                artifact_sha256=item.fingerprint,
                payload_json=item.payload_json,
            )
            for item in components
        ),
    )


def _completed_result(
    authority: _Authority,
    projection: Any,
    persistence: GmailTemporalPersistenceResult,
) -> GmailTemporalRunnerResult:
    if persistence.execution_id is None or persistence.execution_replayed is None:
        raise GmailTemporalRunnerError(
            "production persistence omitted runner execution evidence"
        )
    return GmailTemporalRunnerResult(
        version=GMAIL_TEMPORAL_RUNNER_VERSION,
        disposition="complete_review_projection",
        message_scope_key=authority.source.message_scope_key,
        admission_basis=authority.admission_basis,
        **_authority_counts(authority),
        component_count=_RUN_COUNT,
        artifact_count=len(projection.artifacts),
        cluster_review_count=len(projection.cluster_reviews),
        group_count=len(projection.groups),
        persisted=True,
        head_cleared=False,
        run_id=persistence.run_id,
        head_generation=persistence.head_generation,
        execution_id=persistence.execution_id,
        replayed=persistence.execution_replayed,
        head_changed=persistence.head_changed,
    )


def _authority_counts(authority: _Authority) -> dict[str, int]:
    return {
        "expression_count": len(authority.analysis.expressions),
        "batch_count": len(authority.batch_plan.batches),
        "candidate_count": sum(
            len(item.frontier_candidates) for item in authority.batches
        ),
        "page_count": sum(len(item.page_plan.pages) for item in authority.batches),
    }


def _aware_timestamp(value: str) -> bool:
    parsed = _parse_timestamp(value)
    return (
        parsed is not None
        and parsed.tzinfo is not None
        and parsed.utcoffset() is not None
    )


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed


def _truthy(value: Any) -> bool:
    return value is True or str(value or "").strip().casefold() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
