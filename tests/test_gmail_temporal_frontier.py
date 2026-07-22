from __future__ import annotations

import json
from dataclasses import FrozenInstanceError, asdict, replace

import pytest

from pkm_brain.gmail_temporal_batching import (
    GmailTemporalBatchCaps,
    plan_gmail_temporal_selector_batches,
)
from pkm_brain.gmail_temporal_frontier import (
    GmailTemporalCandidatePageVerdicts,
    GmailTemporalCandidateVerdict,
    GmailTemporalFrontierError,
    build_gmail_temporal_candidate_frontier,
    gmail_temporal_candidate_frontier_payload,
    gmail_temporal_candidate_page_payload,
    plan_gmail_temporal_candidate_pages,
    validate_gmail_temporal_candidate_page_choice,
    validate_gmail_temporal_candidate_verdict_set,
)
from pkm_brain.gmail_temporal_leads import analyze_gmail_temporal_leads


ANCHOR = "2027-05-01T10:00:00-07:00"


def analyze_and_plan(
    text: str,
    *,
    caps: GmailTemporalBatchCaps | None = None,
    chunk_id: str = "synthetic-frontier-message",
):
    analysis = analyze_gmail_temporal_leads(
        text=text,
        message_internal_at=ANCHOR,
        fact_admitted=True,
        chunk_id=chunk_id,
    )
    plan = plan_gmail_temporal_selector_batches(
        text=text,
        analysis=analysis,
        caps=caps,
    )
    return analysis, plan


def page_verdict_rows(
    page_plan,
    *,
    overrides: dict[str, str] | None = None,
):
    values = overrides or {}
    return tuple(
        GmailTemporalCandidatePageVerdicts(
            frontier_fingerprint=page_plan.frontier_fingerprint,
            page_fingerprint=page.page_fingerprint,
            verdicts=tuple(
                GmailTemporalCandidateVerdict(
                    candidate_id=candidate_id,
                    verdict=values.get(candidate_id, "unsupported"),  # type: ignore[arg-type]
                )
                for cluster in page.clusters
                for candidate_id in cluster.candidate_ids
            ),
        )
        for page in page_plan.pages
    )


def parent_cluster_candidate_ids(page_plan):
    ordered: dict[str, list[str]] = {}
    for page in page_plan.pages:
        for cluster in page.clusters:
            ordered.setdefault(cluster.cluster_id, []).extend(cluster.candidate_ids)
    return tuple(
        (cluster_id, tuple(candidate_ids))
        for cluster_id, candidate_ids in ordered.items()
    )


def test_frontier_is_deterministic_bounded_and_validator_backed() -> None:
    analysis, plan = analyze_and_plan(
        "The interview is scheduled for May 14, 2027."
    )
    batch = plan.batches[0]

    first = build_gmail_temporal_candidate_frontier(
        analysis=analysis,
        batch=batch,
    )
    repeated = build_gmail_temporal_candidate_frontier(
        analysis=analysis,
        batch=batch,
    )

    assert first == repeated
    assert first.frontier_fingerprint.startswith("gtf_")
    assert first.batch_fingerprint == batch.manifest.batch_fingerprint
    assert first.analysis_fingerprint == analysis.snapshot_fingerprint
    assert first.complete is True
    assert first.omitted_candidate_mention_count == 0
    assert len(first.candidates) == 2
    assert len({item.binding_id for item in first.candidates}) == 1
    assert {item.lifecycle for item in first.candidates} == {"none", "scheduled"}
    assert all(item.relation == "occurrence" for item in first.candidates)
    assert all(item.kind == "planned" for item in first.candidates)
    assert all(item.normalized_value == "2027-05-14" for item in first.candidates)
    assert all(item.routable is False for item in first.candidates)
    assert first.routable is False
    with pytest.raises(FrozenInstanceError):
        first.complete = False  # type: ignore[misc]


def test_frontier_payload_contains_candidates_not_source_surfaces() -> None:
    private_marker = "PRIVATE-SYNTHETIC-MARKER"
    text = f"The {private_marker} meeting is scheduled for May 14, 2027."
    analysis, plan = analyze_and_plan(text)

    payload = gmail_temporal_candidate_frontier_payload(
        build_gmail_temporal_candidate_frontier(
            analysis=analysis,
            batch=plan.batches[0],
        )
    )
    parsed = json.loads(payload)

    assert parsed["version"] == "gmail_temporal_candidate_frontier_v1"
    assert parsed["candidates"]
    assert private_marker not in payload
    assert all("candidate_id" in item for item in parsed["candidates"])


def test_frontier_choice_resolves_only_current_authorized_candidate() -> None:
    analysis, plan = analyze_and_plan("The meeting is May 14, 2027.")
    batch = plan.batches[0]
    frontier = build_gmail_temporal_candidate_frontier(
        analysis=analysis,
        batch=batch,
    )
    candidate = frontier.candidates[0]
    page = plan_gmail_temporal_candidate_pages(
        analysis=analysis,
        batch=batch,
    ).pages[0]

    citation = validate_gmail_temporal_candidate_page_choice(
        analysis=analysis,
        batch=batch,
        page=page,
        frontier_fingerprint=frontier.frontier_fingerprint,
        page_fingerprint=page.page_fingerprint,
        candidate_id=candidate.candidate_id,
    )

    assert citation.expression_id == candidate.expression_id
    assert citation.subject_mention_id == candidate.subject_mention_id
    with pytest.raises(GmailTemporalFrontierError, match="stale"):
        validate_gmail_temporal_candidate_page_choice(
            analysis=analysis,
            batch=batch,
            page=page,
            frontier_fingerprint="gtf_" + "0" * 64,
            page_fingerprint=page.page_fingerprint,
            candidate_id=candidate.candidate_id,
        )
    with pytest.raises(GmailTemporalFrontierError, match="not presented"):
        validate_gmail_temporal_candidate_page_choice(
            analysis=analysis,
            batch=batch,
            page=page,
            frontier_fingerprint=frontier.frontier_fingerprint,
            page_fingerprint=page.page_fingerprint,
            candidate_id="gtvc_" + "0" * 32,
        )


def test_frontier_recovers_citable_subjects_without_lead_hints() -> None:
    text = (
        "Subject: Orchid Interview\n\n"
        "The meeting is scheduled for May 14, 2027."
    )
    analysis, plan = analyze_and_plan(
        text,
        caps=GmailTemporalBatchCaps(max_lead_hints_per_batch=1),
    )
    batch = plan.batches[0]
    frontier = build_gmail_temporal_candidate_frontier(
        analysis=analysis,
        batch=batch,
    )

    hinted_subjects = {item.mention_id for item in batch.lead_hints}
    candidate_subjects = {item.subject_mention_id for item in frontier.candidates}
    assert candidate_subjects - hinted_subjects
    assert any(
        item.subject_mention_id not in hinted_subjects
        and item.selected_lead_id is None
        for item in frontier.candidates
    )


def test_frontier_reports_candidate_endpoint_truncation_without_silent_loss() -> None:
    text = (
        "Meeting interview workshop conference appointment session event call "
        "visit tour presentation May 14, 2027."
    )
    analysis, plan = analyze_and_plan(
        text,
        caps=GmailTemporalBatchCaps(max_mentions_per_batch=1),
    )
    batch = plan.batches[0]

    frontier = build_gmail_temporal_candidate_frontier(
        analysis=analysis,
        batch=batch,
    )

    assert batch.omitted_mention_count > 0
    assert frontier.omitted_candidate_mention_count > 0
    assert frontier.complete is False
    assert len(frontier.candidates) == 1


def test_frontier_reports_subject_bridge_hidden_by_context_cap() -> None:
    text = (
        "Subject: "
        + ("Update " * 50)
        + "Meeting\n\nWhen: May 14, 2027"
    )
    analysis, plan = analyze_and_plan(text)
    batch = plan.batches[0]

    frontier = build_gmail_temporal_candidate_frontier(
        analysis=analysis,
        batch=batch,
    )

    assert "subject_bridge_context_trimmed" in batch.diagnostics
    assert frontier.candidates == ()
    assert frontier.omitted_candidate_mention_count == 1
    assert frontier.complete is False


def test_frontier_empty_packet_is_complete_only_when_no_subject_was_omitted() -> None:
    analysis, plan = analyze_and_plan("May 14, 2027.")
    frontier = build_gmail_temporal_candidate_frontier(
        analysis=analysis,
        batch=plan.batches[0],
    )

    assert frontier.candidates == ()
    assert frontier.complete is True


def test_frontier_rejects_rebound_analysis_or_mutated_manifest() -> None:
    first_analysis, first_plan = analyze_and_plan(
        "The meeting is May 14, 2027.",
        chunk_id="first",
    )
    second_analysis, _second_plan = analyze_and_plan(
        "The workshop is May 15, 2027.",
        chunk_id="second",
    )
    batch = first_plan.batches[0]

    with pytest.raises(GmailTemporalFrontierError, match="does not match"):
        build_gmail_temporal_candidate_frontier(
            analysis=second_analysis,
            batch=batch,
        )
    forged = replace(
        batch,
        manifest=replace(batch.manifest, mention_ids=("gtl_bad:m1",)),
    )
    with pytest.raises(ValueError):
        build_gmail_temporal_candidate_frontier(
            analysis=first_analysis,
            batch=forged,
        )


def test_page_plan_clusters_reducer_equivalent_title_aliases() -> None:
    analysis, plan = analyze_and_plan(
        "Subject: Q3 Leadership Forum\n\nWhen: May 14, 2027"
    )
    batch = plan.batches[0]
    frontier = build_gmail_temporal_candidate_frontier(
        analysis=analysis,
        batch=batch,
    )

    page_plan = plan_gmail_temporal_candidate_pages(
        analysis=analysis,
        batch=batch,
    )

    assert page_plan.pages
    assert set(page_plan.covered_candidate_ids) == {
        item.candidate_id for item in frontier.candidates
    }
    assert len(page_plan.covered_candidate_ids) == len(
        set(page_plan.covered_candidate_ids)
    )
    assert any(
        len(cluster.subject_mention_ids) > 1
        for page in page_plan.pages
        for cluster in page.clusters
    )


def test_page_plan_pages_every_cluster_without_cap_loss() -> None:
    text = (
        "Meeting interview workshop conference appointment session event call "
        "visit tour presentation May 14, 2027."
    )
    analysis, plan = analyze_and_plan(text)
    batch = plan.batches[0]
    frontier = build_gmail_temporal_candidate_frontier(
        analysis=analysis,
        batch=batch,
    )

    page_plan = plan_gmail_temporal_candidate_pages(
        analysis=analysis,
        batch=batch,
        max_clusters_per_page=2,
    )

    assert len(page_plan.pages) > 1
    assert all(len(page.clusters) <= 2 for page in page_plan.pages)
    assert set(page_plan.covered_candidate_ids) == {
        item.candidate_id for item in frontier.candidates
    }
    assert [item.sequence for item in page_plan.pages] == list(
        range(1, len(page_plan.pages) + 1)
    )
    assert page_plan.complete is frontier.complete is True
    assert all(
        sum(len(cluster.candidate_ids) for cluster in page.clusters)
        <= page_plan.max_candidates_per_page
        for page in page_plan.pages
    )
    assert all(
        len(
            gmail_temporal_candidate_page_payload(
                frontier=frontier,
                page=page,
            ).encode("utf-8")
        )
        <= page_plan.max_payload_bytes
        for page in page_plan.pages
    )


def test_page_payload_is_bound_to_frontier_and_contains_no_source_text() -> None:
    private_marker = "PRIVATE-SYNTHETIC-MARKER"
    analysis, plan = analyze_and_plan(
        f"The {private_marker} meeting is May 14, 2027."
    )
    batch = plan.batches[0]
    frontier = build_gmail_temporal_candidate_frontier(
        analysis=analysis,
        batch=batch,
    )
    page = plan_gmail_temporal_candidate_pages(
        analysis=analysis,
        batch=batch,
    ).pages[0]

    payload = gmail_temporal_candidate_page_payload(
        frontier=frontier,
        page=page,
    )

    assert private_marker not in payload
    assert json.loads(payload)["page_fingerprint"] == page.page_fingerprint
    unrelated_analysis, unrelated_plan = analyze_and_plan(
        "The workshop is May 15, 2027.",
        chunk_id="unrelated",
    )
    unrelated_frontier = build_gmail_temporal_candidate_frontier(
        analysis=unrelated_analysis,
        batch=unrelated_plan.batches[0],
    )
    with pytest.raises(GmailTemporalFrontierError, match="another frontier"):
        gmail_temporal_candidate_page_payload(
            frontier=unrelated_frontier,
            page=page,
        )


def test_page_choice_cannot_cite_candidate_from_sibling_page() -> None:
    text = (
        "Meeting interview workshop conference appointment session event call "
        "visit tour presentation May 14, 2027."
    )
    analysis, plan = analyze_and_plan(text)
    batch = plan.batches[0]
    page_plan = plan_gmail_temporal_candidate_pages(
        analysis=analysis,
        batch=batch,
        max_clusters_per_page=1,
    )
    assert len(page_plan.pages) > 1
    first, second = page_plan.pages[:2]
    sibling_candidate_id = second.clusters[0].candidate_ids[0]

    with pytest.raises(GmailTemporalFrontierError, match="not presented"):
        validate_gmail_temporal_candidate_page_choice(
            analysis=analysis,
            batch=batch,
            page=first,
            frontier_fingerprint=page_plan.frontier_fingerprint,
            page_fingerprint=first.page_fingerprint,
            candidate_id=sibling_candidate_id,
        )
    citation = validate_gmail_temporal_candidate_page_choice(
        analysis=analysis,
        batch=batch,
        page=second,
        frontier_fingerprint=page_plan.frontier_fingerprint,
        page_fingerprint=second.page_fingerprint,
        candidate_id=sibling_candidate_id,
    )
    assert citation.batch_fingerprint == batch.manifest.batch_fingerprint


def test_page_plan_splits_lifecycle_variants_under_count_and_byte_bounds() -> None:
    text = (
        "Meeting interview workshop conference appointment session event call "
        "visit tour presentation are scheduled for May 14, 2027."
    )
    analysis, plan = analyze_and_plan(text)
    batch = plan.batches[0]
    frontier = build_gmail_temporal_candidate_frontier(
        analysis=analysis,
        batch=batch,
    )

    page_plan = plan_gmail_temporal_candidate_pages(
        analysis=analysis,
        batch=batch,
        max_clusters_per_page=4,
        max_candidates_per_page=5,
        max_payload_bytes=5_500,
    )

    assert len(frontier.candidates) > page_plan.max_candidates_per_page
    assert len(page_plan.pages) > 1
    assert set(page_plan.covered_candidate_ids) == {
        item.candidate_id for item in frontier.candidates
    }
    for page in page_plan.pages:
        assert sum(len(item.candidate_ids) for item in page.clusters) <= 5
        assert len(
            gmail_temporal_candidate_page_payload(
                frontier=frontier,
                page=page,
            ).encode("utf-8")
        ) <= 5_500


def test_page_plan_fails_closed_when_one_candidate_exceeds_byte_bound() -> None:
    analysis, plan = analyze_and_plan("The meeting is May 14, 2027.")

    with pytest.raises(GmailTemporalFrontierError, match="exceeds"):
        plan_gmail_temporal_candidate_pages(
            analysis=analysis,
            batch=plan.batches[0],
            max_payload_bytes=1,
        )


def test_split_alias_cluster_has_page_unique_decision_units() -> None:
    analysis, plan = analyze_and_plan(
        "The interview is scheduled for May 14, 2027."
    )

    page_plan = plan_gmail_temporal_candidate_pages(
        analysis=analysis,
        batch=plan.batches[0],
        max_candidates_per_page=1,
    )

    assert len(page_plan.pages) == 2
    first = page_plan.pages[0].clusters[0]
    second = page_plan.pages[1].clusters[0]
    assert first.cluster_id == second.cluster_id
    assert first.decision_unit_id != second.decision_unit_id


def test_verdict_set_requires_complete_pages_and_projects_support() -> None:
    analysis, plan = analyze_and_plan(
        "The interview is scheduled for May 14, 2027."
    )
    batch = plan.batches[0]
    page_plan = plan_gmail_temporal_candidate_pages(
        analysis=analysis,
        batch=batch,
    )
    candidate_ids = page_plan.covered_candidate_ids
    rows = page_verdict_rows(
        page_plan,
        overrides={candidate_ids[0]: "supported"},
    )

    result = validate_gmail_temporal_candidate_verdict_set(
        analysis=analysis,
        batch=batch,
        plan=page_plan,
        rows=tuple(reversed(rows)),
    )

    assert result.version == "gmail_temporal_candidate_verdict_set_v2"
    assert result.supported_candidate_ids == (candidate_ids[0],)
    assert result.uncertain_clusters == ()
    assert result.unsupported_candidate_count == len(candidate_ids) - 1
    assert len(result.supported_citations) == 1
    assert result.complete is True
    assert result.requires_defer is False
    assert result.routable is False


def test_verdict_set_uncertain_or_incomplete_frontier_forces_defer() -> None:
    analysis, plan = analyze_and_plan("The meeting is May 14, 2027.")
    batch = plan.batches[0]
    page_plan = plan_gmail_temporal_candidate_pages(
        analysis=analysis,
        batch=batch,
    )
    rows = page_verdict_rows(
        page_plan,
        overrides={page_plan.covered_candidate_ids[0]: "uncertain"},
    )

    result = validate_gmail_temporal_candidate_verdict_set(
        analysis=analysis,
        batch=batch,
        plan=page_plan,
        rows=rows,
    )
    assert result.supported_candidate_ids == ()
    assert result.supported_citations == ()
    assert len(result.uncertain_clusters) == 1
    uncertainty = result.uncertain_clusters[0]
    assert uncertainty.version == "gmail_temporal_candidate_cluster_uncertainty_v1"
    assert uncertainty.plausible_candidate_ids == (
        page_plan.covered_candidate_ids[0],
    )
    assert uncertainty.reason == "model_uncertain"
    assert uncertainty.requires_defer is True
    assert uncertainty.routable is False
    assert result.requires_defer is True

    dense_analysis, dense_plan = analyze_and_plan(
        "Meeting interview workshop May 14, 2027.",
        caps=GmailTemporalBatchCaps(max_mentions_per_batch=1),
    )
    dense_batch = dense_plan.batches[0]
    dense_page_plan = plan_gmail_temporal_candidate_pages(
        analysis=dense_analysis,
        batch=dense_batch,
    )
    dense_result = validate_gmail_temporal_candidate_verdict_set(
        analysis=dense_analysis,
        batch=dense_batch,
        plan=dense_page_plan,
        rows=page_verdict_rows(dense_page_plan),
    )
    assert dense_page_plan.complete is False
    assert dense_result.requires_defer is True

    empty_analysis, empty_plan = analyze_and_plan("May 14, 2027.")
    empty_batch = empty_plan.batches[0]
    empty_page_plan = plan_gmail_temporal_candidate_pages(
        analysis=empty_analysis,
        batch=empty_batch,
    )
    empty_result = validate_gmail_temporal_candidate_verdict_set(
        analysis=empty_analysis,
        batch=empty_batch,
        plan=empty_page_plan,
        rows=(),
    )
    assert empty_page_plan.complete is True
    assert empty_result.requires_defer is True


def test_supported_candidate_preserves_deterministic_defer_requirement() -> None:
    analysis, plan = analyze_and_plan("The meeting is scheduled for 7/8/2027.")
    batch = plan.batches[0]
    page_plan = plan_gmail_temporal_candidate_pages(
        analysis=analysis,
        batch=batch,
    )
    candidate_id = page_plan.covered_candidate_ids[0]

    result = validate_gmail_temporal_candidate_verdict_set(
        analysis=analysis,
        batch=batch,
        plan=page_plan,
        rows=page_verdict_rows(
            page_plan,
            overrides={candidate_id: "supported"},
        ),
    )

    assert result.supported_candidate_ids == (candidate_id,)
    assert result.requires_defer is True


def test_verdict_set_rejects_omitted_pages_and_candidates() -> None:
    text = (
        "Meeting interview workshop conference appointment session event call "
        "visit tour presentation are scheduled for May 14, 2027."
    )
    analysis, plan = analyze_and_plan(text)
    batch = plan.batches[0]
    page_plan = plan_gmail_temporal_candidate_pages(
        analysis=analysis,
        batch=batch,
        max_clusters_per_page=1,
    )
    rows = page_verdict_rows(page_plan)
    assert len(rows) > 1

    with pytest.raises(GmailTemporalFrontierError, match="exactly"):
        validate_gmail_temporal_candidate_verdict_set(
            analysis=analysis,
            batch=batch,
            plan=page_plan,
            rows=rows[:-1],
        )
    malformed_page = replace(rows[0], page_fingerprint=[])  # type: ignore[arg-type]
    with pytest.raises(GmailTemporalFrontierError, match="malformed"):
        validate_gmail_temporal_candidate_verdict_set(
            analysis=analysis,
            batch=batch,
            plan=page_plan,
            rows=(malformed_page, *rows[1:]),
        )
    first = rows[0]
    missing_candidate = replace(first, verdicts=first.verdicts[:-1])
    with pytest.raises(GmailTemporalFrontierError, match="exactly once"):
        validate_gmail_temporal_candidate_verdict_set(
            analysis=analysis,
            batch=batch,
            plan=page_plan,
            rows=(missing_candidate, *rows[1:]),
        )


def test_verdict_set_all_unsupported_has_no_projection_or_uncertainty() -> None:
    analysis, plan = analyze_and_plan("The meeting is May 14, 2027.")
    batch = plan.batches[0]
    page_plan = plan_gmail_temporal_candidate_pages(
        analysis=analysis,
        batch=batch,
    )

    result = validate_gmail_temporal_candidate_verdict_set(
        analysis=analysis,
        batch=batch,
        plan=page_plan,
        rows=page_verdict_rows(page_plan),
    )

    assert result.supported_candidate_ids == ()
    assert result.supported_citations == ()
    assert result.uncertain_clusters == ()
    assert result.unsupported_candidate_count == len(page_plan.covered_candidate_ids)
    assert result.requires_defer is False


def test_supported_plus_uncertain_cluster_becomes_model_uncertainty() -> None:
    analysis, plan = analyze_and_plan(
        "The interview is scheduled for May 14, 2027."
    )
    batch = plan.batches[0]
    page_plan = plan_gmail_temporal_candidate_pages(
        analysis=analysis,
        batch=batch,
    )
    ((cluster_id, candidate_ids),) = parent_cluster_candidate_ids(page_plan)
    assert len(candidate_ids) == 2

    result = validate_gmail_temporal_candidate_verdict_set(
        analysis=analysis,
        batch=batch,
        plan=page_plan,
        rows=page_verdict_rows(
            page_plan,
            overrides={
                candidate_ids[0]: "supported",
                candidate_ids[1]: "uncertain",
            },
        ),
    )

    assert result.supported_candidate_ids == ()
    assert result.supported_citations == ()
    assert result.unsupported_candidate_count == 0
    assert len(result.uncertain_clusters) == 1
    uncertainty = result.uncertain_clusters[0]
    assert uncertainty.cluster_id == cluster_id
    assert uncertainty.plausible_candidate_ids == candidate_ids
    assert uncertainty.reason == "model_uncertain"
    assert result.requires_defer is True


def test_multiple_supported_candidates_become_conflict_uncertainty() -> None:
    analysis, plan = analyze_and_plan(
        "The interview is scheduled for May 14, 2027."
    )
    batch = plan.batches[0]
    page_plan = plan_gmail_temporal_candidate_pages(
        analysis=analysis,
        batch=batch,
    )
    ((cluster_id, candidate_ids),) = parent_cluster_candidate_ids(page_plan)
    assert len(candidate_ids) == 2

    result = validate_gmail_temporal_candidate_verdict_set(
        analysis=analysis,
        batch=batch,
        plan=page_plan,
        rows=page_verdict_rows(
            page_plan,
            overrides={candidate_id: "supported" for candidate_id in candidate_ids},
        ),
    )

    assert result.supported_candidate_ids == ()
    assert result.supported_citations == ()
    assert len(result.uncertain_clusters) == 1
    assert (
        result.uncertain_clusters[0].version
        == "gmail_temporal_candidate_cluster_uncertainty_v1"
    )
    assert result.uncertain_clusters[0].cluster_id == cluster_id
    assert result.uncertain_clusters[0].plausible_candidate_ids == candidate_ids
    assert (
        result.uncertain_clusters[0].reason
        == "conflicting_supported_candidates"
    )
    assert result.requires_defer is True


def test_alias_cluster_uncertainty_aggregates_across_page_fragments() -> None:
    analysis, plan = analyze_and_plan(
        "Subject: Q3 Leadership Forum\n\n"
        "The Q3 Leadership Forum is scheduled for May 14, 2027."
    )
    batch = plan.batches[0]
    page_plan = plan_gmail_temporal_candidate_pages(
        analysis=analysis,
        batch=batch,
        max_candidates_per_page=1,
    )
    clusters = parent_cluster_candidate_ids(page_plan)
    alias_cluster_ids = {
        cluster.cluster_id
        for page in page_plan.pages
        for cluster in page.clusters
        if len(cluster.subject_mention_ids) > 1
    }
    assert alias_cluster_ids
    cluster_id, candidate_ids = next(
        item
        for item in clusters
        if item[0] in alias_cluster_ids and len(item[1]) >= 3
    )
    pages_by_candidate = {
        candidate_id: page.page_fingerprint
        for page in page_plan.pages
        for cluster in page.clusters
        for candidate_id in cluster.candidate_ids
    }
    assert pages_by_candidate[candidate_ids[0]] != pages_by_candidate[candidate_ids[1]]
    rows = page_verdict_rows(
        page_plan,
        overrides={
            candidate_ids[0]: "supported",
            candidate_ids[1]: "supported",
            candidate_ids[2]: "uncertain",
        },
    )
    reversed_rows_and_verdicts = tuple(
        replace(row, verdicts=tuple(reversed(row.verdicts)))
        for row in reversed(rows)
    )

    result = validate_gmail_temporal_candidate_verdict_set(
        analysis=analysis,
        batch=batch,
        plan=page_plan,
        rows=reversed_rows_and_verdicts,
    )

    assert result.supported_citations == ()
    assert len(result.uncertain_clusters) == 1
    uncertainty = result.uncertain_clusters[0]
    assert uncertainty.cluster_id == cluster_id
    assert uncertainty.plausible_candidate_ids == candidate_ids[:3]
    assert uncertainty.reason == "model_uncertain"


def test_uncertainty_sidecars_follow_canonical_parent_cluster_order() -> None:
    analysis, plan = analyze_and_plan(
        "Meeting interview workshop are scheduled for May 14, 2027."
    )
    batch = plan.batches[0]
    page_plan = plan_gmail_temporal_candidate_pages(
        analysis=analysis,
        batch=batch,
        max_clusters_per_page=1,
        max_candidates_per_page=1,
    )
    clusters = tuple(
        item for item in parent_cluster_candidate_ids(page_plan) if len(item[1]) >= 2
    )
    assert len(clusters) >= 2
    first_cluster, second_cluster = clusters[:2]
    overrides = {
        first_cluster[1][0]: "supported",
        first_cluster[1][1]: "supported",
        second_cluster[1][0]: "uncertain",
    }

    result = validate_gmail_temporal_candidate_verdict_set(
        analysis=analysis,
        batch=batch,
        plan=page_plan,
        rows=tuple(reversed(page_verdict_rows(page_plan, overrides=overrides))),
    )

    assert tuple(item.cluster_id for item in result.uncertain_clusters) == (
        first_cluster[0],
        second_cluster[0],
    )
    assert result.uncertain_clusters[0].plausible_candidate_ids == first_cluster[1]
    assert result.uncertain_clusters[1].plausible_candidate_ids == (
        second_cluster[1][0],
    )


def test_exact_cluster_remains_citable_beside_uncertain_cluster() -> None:
    analysis, plan = analyze_and_plan(
        "Meeting and workshop are scheduled for May 14, 2027."
    )
    batch = plan.batches[0]
    page_plan = plan_gmail_temporal_candidate_pages(
        analysis=analysis,
        batch=batch,
        max_candidates_per_page=1,
    )
    clusters = parent_cluster_candidate_ids(page_plan)
    assert len(clusters) >= 2
    exact_cluster, uncertain_cluster = clusters[:2]
    exact_candidate_id = exact_cluster[1][0]
    uncertain_candidate_id = uncertain_cluster[1][0]

    result = validate_gmail_temporal_candidate_verdict_set(
        analysis=analysis,
        batch=batch,
        plan=page_plan,
        rows=page_verdict_rows(
            page_plan,
            overrides={
                exact_candidate_id: "supported",
                uncertain_candidate_id: "uncertain",
            },
        ),
    )

    assert result.supported_candidate_ids == (exact_candidate_id,)
    assert len(result.supported_citations) == 1
    assert result.uncertain_clusters[0].cluster_id == uncertain_cluster[0]
    assert result.uncertain_clusters[0].plausible_candidate_ids == (
        uncertain_candidate_id,
    )
    assert result.requires_defer is True


def test_verdict_set_v2_serialization_separates_raw_rows_from_sidecars() -> None:
    analysis, plan = analyze_and_plan(
        "The interview is scheduled for May 14, 2027."
    )
    batch = plan.batches[0]
    page_plan = plan_gmail_temporal_candidate_pages(
        analysis=analysis,
        batch=batch,
    )
    candidate_ids = page_plan.covered_candidate_ids
    rows = page_verdict_rows(
        page_plan,
        overrides={
            candidate_ids[0]: "supported",
            candidate_ids[1]: "uncertain",
        },
    )

    raw_row = asdict(rows[0])
    assert set(raw_row) == {
        "frontier_fingerprint",
        "page_fingerprint",
        "verdicts",
        "routable",
    }
    assert set(raw_row["verdicts"][0]) == {
        "candidate_id",
        "verdict",
        "routable",
    }
    result = validate_gmail_temporal_candidate_verdict_set(
        analysis=analysis,
        batch=batch,
        plan=page_plan,
        rows=rows,
    )
    serialized = asdict(result)

    assert serialized["version"] == "gmail_temporal_candidate_verdict_set_v2"
    assert "uncertain_candidate_ids" not in serialized
    assert "uncertain_citations" not in serialized
    assert serialized["supported_citations"] == ()
    assert serialized["uncertain_clusters"][0] == {
        "version": "gmail_temporal_candidate_cluster_uncertainty_v1",
        "cluster_id": result.uncertain_clusters[0].cluster_id,
        "plausible_candidate_ids": candidate_ids,
        "reason": "model_uncertain",
        "requires_defer": True,
        "routable": False,
    }


@pytest.mark.parametrize(
    ("text", "expected_lifecycle"),
    (
        ("The meeting scheduled for May 14, 2027 was cancelled.", "cancelled"),
        ("The meeting on May 14, 2027 was completed.", "completed"),
    ),
)
def test_terminal_lifecycle_candidates_preserve_terminal_semantics(
    text: str,
    expected_lifecycle: str,
) -> None:
    analysis, plan = analyze_and_plan(text)
    frontier = build_gmail_temporal_candidate_frontier(
        analysis=analysis,
        batch=plan.batches[0],
    )

    terminal = next(
        item for item in frontier.candidates if item.lifecycle == expected_lifecycle
    )
    assert (terminal.relation, terminal.kind) == ("unspecified", "unspecified")
    assert terminal.lifecycle_mention_id is not None
    assert terminal.requires_defer is False


def test_unresolved_reschedule_variants_never_become_precise() -> None:
    analysis, plan = analyze_and_plan(
        "The meeting was rescheduled from May 14, 2027 to May 16, 2027."
    )
    frontiers = [
        build_gmail_temporal_candidate_frontier(
            analysis=analysis,
            batch=batch,
        )
        for batch in plan.batches
    ]

    rescheduled = [
        candidate
        for frontier in frontiers
        for candidate in frontier.candidates
        if candidate.lifecycle_mention_id is not None
    ]
    assert len(rescheduled) == 2
    assert all(item.lifecycle == "unknown" for item in rescheduled)
    assert all((item.relation, item.kind) == ("unspecified", "unspecified") for item in rescheduled)
    assert all(item.requires_defer is True for item in rescheduled)


def test_unrelated_lifecycle_cue_is_not_crossed_into_other_segment() -> None:
    analysis, plan = analyze_and_plan(
        "The meeting is scheduled for May 14, 2027. The workshop was cancelled."
    )
    frontier = build_gmail_temporal_candidate_frontier(
        analysis=analysis,
        batch=plan.batches[0],
    )

    assert {item.lifecycle for item in frontier.candidates} == {"none", "scheduled"}


@pytest.mark.parametrize("value", (0, -1, True, 1.5))
def test_page_plan_rejects_invalid_cluster_cap(value: object) -> None:
    analysis, plan = analyze_and_plan("The meeting is May 14, 2027.")

    with pytest.raises(ValueError, match="positive integer"):
        plan_gmail_temporal_candidate_pages(
            analysis=analysis,
            batch=plan.batches[0],
            max_clusters_per_page=value,  # type: ignore[arg-type]
        )
