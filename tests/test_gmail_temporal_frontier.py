from __future__ import annotations

import json
from dataclasses import FrozenInstanceError, asdict, replace

import pytest

from pkm_brain.gmail_temporal_batching import (
    GMAIL_TEMPORAL_SINGLETON_EVENT_FALLBACK_DIAGNOSTIC,
    GmailTemporalBatchCaps,
    plan_gmail_temporal_selector_batches,
)
from pkm_brain.gmail_temporal_frontier import (
    GmailTemporalCandidatePageVerdicts,
    GmailTemporalCandidateVerdict,
    GmailTemporalCandidateEnsembleVerdictSet,
    GmailTemporalFrontierError,
    _canonicalize_scope_conflicted_lifecycle_review,
    _explicit_lifecycle_subsumes_base,
    _is_direct_actual_endpoint,
    build_gmail_temporal_candidate_frontier,
    gmail_temporal_candidate_frontier_payload,
    gmail_temporal_candidate_ensemble_policy_fingerprint,
    gmail_temporal_candidate_page_payload,
    plan_gmail_temporal_candidate_pages,
    validate_gmail_temporal_candidate_page_choice,
    validate_gmail_temporal_candidate_ensemble_verdict_set,
    validate_gmail_temporal_candidate_verdict_set,
)
from pkm_brain.gmail_temporal_leads import analyze_gmail_temporal_leads
from pkm_brain.gmail_temporal_selection import GMAIL_TEMPORAL_HARD_SCOPE_BLOCKERS


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
    analysis, plan = analyze_and_plan("The interview is scheduled for May 14, 2027.")
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
    assert len(first.candidates) == 1
    assert len({item.binding_id for item in first.candidates}) == 1
    assert {item.lifecycle for item in first.candidates} == {"scheduled"}
    assert all(item.relation == "occurrence" for item in first.candidates)
    assert all(item.kind == "planned" for item in first.candidates)
    assert all(item.normalized_value == "2027-05-14" for item in first.candidates)
    assert all(item.routable is False for item in first.candidates)
    assert first.routable is False
    with pytest.raises(FrozenInstanceError):
        first.complete = False  # type: ignore[misc]


@pytest.mark.parametrize("lifecycle", ("cancelled", "completed"))
def test_exact_terminal_lifecycle_omits_redundant_base(lifecycle: str) -> None:
    analysis, plan = analyze_and_plan(f"The meeting was {lifecycle} on May 14, 2027.")
    frontier = build_gmail_temporal_candidate_frontier(
        analysis=analysis,
        batch=plan.batches[0],
    )

    assert {item.lifecycle for item in frontier.candidates} == {lifecycle}


def test_deferred_lifecycle_retains_base_for_recall() -> None:
    analysis, plan = analyze_and_plan(
        "The interview is scheduled for May 14, 2027 at 4:30 PM."
    )
    frontier = build_gmail_temporal_candidate_frontier(
        analysis=analysis,
        batch=plan.batches[0],
    )

    assert {item.lifecycle for item in frontier.candidates} == {"none", "scheduled"}
    assert all(item.requires_defer is True for item in frontier.candidates)


@pytest.mark.parametrize(
    ("admitted", "rescue", "expected_rescue_blocker"),
    (
        (True, False, False),
        (False, True, True),
    ),
)
def test_singleton_cross_segment_event_candidate_is_always_deferred(
    admitted: bool,
    rescue: bool,
    expected_rescue_blocker: bool,
) -> None:
    text = "The workshop update is ready. May 14, 2027."
    analysis = analyze_gmail_temporal_leads(
        text=text,
        message_internal_at=ANCHOR,
        fact_admitted=admitted,
        temporal_review_rescue=rescue,
        chunk_id="synthetic-singleton-cross-segment",
    )
    plan = plan_gmail_temporal_selector_batches(text=text, analysis=analysis)
    batch = plan.batches[0]

    frontier = build_gmail_temporal_candidate_frontier(
        analysis=analysis,
        batch=batch,
    )

    assert GMAIL_TEMPORAL_SINGLETON_EVENT_FALLBACK_DIAGNOSTIC in batch.diagnostics
    assert frontier.complete is True
    assert len(frontier.candidates) == 1
    candidate = frontier.candidates[0]
    assert candidate.subject_mention_id == analysis.leads[0].mention_id
    assert candidate.selected_lead_id == analysis.leads[0].lead_id
    assert candidate.supporting_lead_present is True
    assert candidate.relation == "occurrence"
    assert candidate.lifecycle == "none"
    assert candidate.requires_defer is True
    assert candidate.routable is False
    assert "field_near_review_only" in candidate.blockers
    assert (
        "temporal_review_rescue_only" in candidate.blockers
    ) is expected_rescue_blocker
    assert not GMAIL_TEMPORAL_HARD_SCOPE_BLOCKERS.intersection(candidate.blockers)

    page_plan = plan_gmail_temporal_candidate_pages(
        analysis=analysis,
        batch=batch,
    )
    verdict_set = validate_gmail_temporal_candidate_verdict_set(
        analysis=analysis,
        batch=batch,
        plan=page_plan,
        rows=page_verdict_rows(
            page_plan,
            overrides={candidate.candidate_id: "supported"},
        ),
    )
    assert verdict_set.supported_candidate_ids == (candidate.candidate_id,)
    assert verdict_set.requires_defer is True
    assert all(item.routable is False for item in verdict_set.supported_citations)


def test_singleton_cross_segment_action_does_not_enter_frontier() -> None:
    text = "Please submit the form. May 14, 2027."
    analysis, plan = analyze_and_plan(text)
    batch = plan.batches[0]

    frontier = build_gmail_temporal_candidate_frontier(
        analysis=analysis,
        batch=batch,
    )

    assert GMAIL_TEMPORAL_SINGLETON_EVENT_FALLBACK_DIAGNOSTIC not in batch.diagnostics
    assert frontier.candidates == ()


def test_terminal_lifecycle_keeps_distinct_direct_actual_occurrence() -> None:
    analysis, plan = analyze_and_plan(
        "The meeting took place on May 14, 2027 and was completed."
    )
    frontier = build_gmail_temporal_candidate_frontier(
        analysis=analysis,
        batch=plan.batches[0],
    )

    assert {item.lifecycle for item in frontier.candidates} == {"none", "completed"}
    occurrence = next(item for item in frontier.candidates if item.lifecycle == "none")
    assert (occurrence.relation, occurrence.kind) == ("occurrence", "actual")
    assert occurrence.requires_defer is False


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
    text = "Subject: Orchid Interview\n\nThe meeting is scheduled for May 14, 2027."
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
        item.subject_mention_id not in hinted_subjects and item.selected_lead_id is None
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


def test_frontier_recovers_same_segment_event_outside_trimmed_context() -> None:
    text = (
        "Subject: FYI\n\nMeeting "
        + ("context " * 700)
        + "is scheduled for May 14, 2027."
    )
    analysis, plan = analyze_and_plan(text)
    batch = plan.batches[0]

    frontier = build_gmail_temporal_candidate_frontier(
        analysis=analysis,
        batch=batch,
    )

    assert "local_context_trimmed" in batch.diagnostics
    event = next(
        item
        for item in batch.mentions
        if item.mention_type == "event" and item.start == text.index("Meeting")
    )
    assert batch.omitted_mention_count == 0
    recovered = tuple(
        item
        for item in frontier.candidates
        if item.subject_mention_id == event.mention_id
    )
    assert recovered
    assert all(item.requires_defer is True for item in recovered)
    assert frontier.omitted_candidate_mention_count == 0
    assert frontier.complete is True


def test_frontier_is_complete_when_artifacts_yield_to_citable_bridges() -> None:
    text = (
        "Subject: Cancelled Planning Meeting\n\n"
        "Email calendar invitation attachment document link agenda "
        "Meeting May 14, 2027."
    )
    analysis, plan = analyze_and_plan(
        text,
        caps=GmailTemporalBatchCaps(max_mentions_per_batch=4),
    )

    frontier = build_gmail_temporal_candidate_frontier(
        analysis=analysis,
        batch=plan.batches[0],
    )

    assert frontier.candidates
    assert frontier.omitted_candidate_mention_count == 0
    assert frontier.complete is True


def test_frontier_reports_subject_bridge_hidden_by_context_cap() -> None:
    text = "Subject: " + ("Update " * 50) + "Meeting\n\nWhen: May 14, 2027"
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


def test_missing_benchmark_predicates_produce_nonempty_frontiers() -> None:
    texts = (
        "The new benefits policy becomes effective August 1, 2027.",
        "Registration opens August 12, 2027 and closes August 20, 2027.",
        "The policy effective date is August 1, 2027.",
        "Applications open August 12, 2027.",
        "The policy is in force beginning August 1, 2027.",
        "Enrollment begins August 12, 2027.",
        "Registration is open from August 12, 2027.",
        "Applications are open from August 12, 2027.",
        "Applications will be open from August 12, 2027.",
        "Applications remain open from August 12, 2027.",
        "Enrollment starts August 12, 2027.",
        "The rule applies as of August 1, 2027.",
    )
    expected_frontier_counts = (1, 2, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1)

    for text, expected_count in zip(texts, expected_frontier_counts, strict=True):
        analysis, plan = analyze_and_plan(text)
        frontiers = tuple(
            build_gmail_temporal_candidate_frontier(
                analysis=analysis,
                batch=batch,
            )
            for batch in plan.batches
        )

        assert len(frontiers) == expected_count
        assert all(frontier.candidates for frontier in frontiers)
        assert all(
            candidate.requires_defer
            for frontier in frontiers
            for candidate in frontier.candidates
        )


@pytest.mark.parametrize(
    "text",
    (
        "The benefits policy effective date is August 1, 2027.",
        "The coverage plan effective date is August 1, 2027.",
        "Our benefits plan applies as of August 1, 2027.",
        "Your coverage policy is in force beginning August 1, 2027.",
    ),
)
def test_effective_boundary_allowlisted_compounds_produce_frontiers(
    text: str,
) -> None:
    analysis, plan = analyze_and_plan(text)
    frontiers = tuple(
        build_gmail_temporal_candidate_frontier(
            analysis=analysis,
            batch=batch,
        )
        for batch in plan.batches
    )

    assert any(item.mention_type == "event_predicate" for item in analysis.mentions)
    assert analysis.leads
    assert frontiers
    assert all(frontier.candidates for frontier in frontiers)
    assert all(
        candidate.requires_defer
        for frontier in frontiers
        for candidate in frontier.candidates
    )


@pytest.mark.parametrize(
    "text",
    (
        "Registration is open\n\n          from August 12, 2027.",
        "The store is open from August 12, 2027.",
        "Keep applications open on August 12, 2027.",
        "Keep applications open from August 12, 2027.",
        "We reviewed applications open on August 12, 2027.",
        "I use applications open on August 12, 2027 for QA.",
        "Applications are open for QA on August 12, 2027.",
        "The report will be open from August 12, 2027.",
        "The application form starts August 12, 2027.",
        "The application starts with questionnaire on August 12, 2027.",
        "Applications are open\n\n          from August 12, 2027.",
        "Applications for QA open August 12, 2027 reports",
        "Applications are open from August 12, 2027 reports.",
        "Applications will be open from August 12, 2027 reports.",
        "Applications remain open from August 12, 2027 reports.",
        "Enrollment starts August 12, 2027 questionnaire.",
    ),
)
def test_bounded_intake_predicates_do_not_create_unsafe_frontiers(
    text: str,
) -> None:
    analysis, plan = analyze_and_plan(text)

    assert not any(item.mention_type == "event_predicate" for item in analysis.mentions)
    assert analysis.leads == ()
    frontiers = tuple(
        build_gmail_temporal_candidate_frontier(
            analysis=analysis,
            batch=batch,
        )
        for batch in plan.batches
    )
    assert frontiers
    assert all(frontier.candidates == () for frontier in frontiers)


@pytest.mark.parametrize(
    "text",
    (
        "The discount for the policy applies as of August 1, 2027.",
        "The report about the contract is in force beginning August 1, 2027.",
        "The summary of the policy changes applies as of August 1, 2027.",
        "The memo about our terms applies as of August 1, 2027.",
        "The benefits policy report applies as of August 1, 2027.",
        "The coverage plan summary is in force beginning August 1, 2027.",
        "The discount for the benefits policy applies as of August 1, 2027.",
        ("The report about the coverage plan is in force beginning August 1, 2027."),
        "The policy applies as of August 1, 2027 reports indicate otherwise.",
        "The benefits plan effective date is August 1, 2027 reports show.",
        ("The coverage policy is in force beginning August 1, 2027 reports show."),
    ),
)
def test_effective_boundary_modifier_heads_do_not_create_unsafe_frontiers(
    text: str,
) -> None:
    analysis, plan = analyze_and_plan(text)

    assert not any(item.mention_type == "event_predicate" for item in analysis.mentions)
    assert analysis.leads == ()
    frontiers = tuple(
        build_gmail_temporal_candidate_frontier(
            analysis=analysis,
            batch=batch,
        )
        for batch in plan.batches
    )
    assert frontiers
    assert all(frontier.candidates == () for frontier in frontiers)


@pytest.mark.parametrize(
    "text",
    (
        "The clerk opens the report on August 12, 2027.",
        "We opened the application on August 12, 2027.",
        "The application report opens August 12, 2027.",
        "Registration opens August 12, 2027 reports show.",
    ),
)
def test_opening_inflection_lookalikes_have_no_frontier_candidates(
    text: str,
) -> None:
    analysis, plan = analyze_and_plan(text)
    frontiers = tuple(
        build_gmail_temporal_candidate_frontier(
            analysis=analysis,
            batch=batch,
        )
        for batch in plan.batches
    )

    assert not any(item.mention_type == "event_predicate" for item in analysis.mentions)
    assert analysis.leads == ()
    assert frontiers
    assert all(frontier.candidates == () for frontier in frontiers)


@pytest.mark.parametrize(
    ("text", "surface", "expected_kind", "expected_date"),
    (
        (
            "The portal opens August 12, 2027.",
            "opens",
            "planned",
            "2027-08-12",
        ),
        (
            "The portal will open August 12, 2027.",
            "will open",
            "planned",
            "2027-08-12",
        ),
        (
            "The portal opened August 12, 2027.",
            "opened",
            "actual",
            "2027-08-12",
        ),
        (
            "Registration opens August 12, 2027 and closes August 20, 2027.",
            "opens",
            "planned",
            "2027-08-12",
        ),
    ),
)
def test_bounded_opening_inflections_reach_forced_defer_frontier(
    text: str,
    surface: str,
    expected_kind: str,
    expected_date: str,
) -> None:
    analysis, plan = analyze_and_plan(text)
    predicate = next(
        item
        for item in analysis.mentions
        if item.mention_type == "event_predicate"
        and text[item.start : item.end] == surface
    )
    candidates = tuple(
        candidate
        for batch in plan.batches
        for candidate in build_gmail_temporal_candidate_frontier(
            analysis=analysis,
            batch=batch,
        ).candidates
        if candidate.subject_mention_id == predicate.mention_id
        and candidate.normalized_value == expected_date
    )

    assert candidates
    assert {item.relation for item in candidates} == {"occurrence"}
    assert {item.kind for item in candidates} == {expected_kind}
    assert all(item.requires_defer is True for item in candidates)


def test_page_plan_clusters_adjacent_compound_event_nouns() -> None:
    text = "The review meeting is scheduled for May 14, 2027."
    analysis, plan = analyze_and_plan(text)
    page_plan = plan_gmail_temporal_candidate_pages(
        analysis=analysis,
        batch=plan.batches[0],
    )
    event_ids = {
        item.mention_id
        for item in analysis.mentions
        if item.mention_type == "event"
        and text[item.start : item.end].casefold() in {"review", "meeting"}
    }

    assert any(
        event_ids.issubset(cluster.subject_mention_ids)
        for page in page_plan.pages
        for cluster in page.clusters
    )


@pytest.mark.parametrize(
    ("body", "expected_same_cluster"),
    (
        ("Orchid Interview is scheduled for May 14, 2027.", True),
        ("ORCHID   INTERVIEW is scheduled for May 14, 2027.", True),
        ("The interview is scheduled for May 14, 2027.", False),
    ),
)
def test_subject_title_alias_requires_exact_local_phrase(
    body: str,
    expected_same_cluster: bool,
) -> None:
    text = f"Subject: Orchid Interview\n\n{body}"
    analysis, plan = analyze_and_plan(text)
    page_plan = plan_gmail_temporal_candidate_pages(
        analysis=analysis,
        batch=plan.batches[0],
    )
    title = next(
        item
        for item in analysis.mentions
        if item.mention_type == "event_title_candidate"
    )
    body_event = next(
        item
        for item in analysis.mentions
        if item.mention_type == "event" and item.field == "body"
    )
    same_cluster = any(
        {title.mention_id, body_event.mention_id}.issubset(cluster.subject_mention_ids)
        for page in page_plan.pages
        for cluster in page.clusters
    )

    assert same_cluster is expected_same_cluster


@pytest.mark.parametrize(
    "subject",
    (
        "Atlas Interview Update",
        "Reminder: Atlas Interview",
        "Atlas Interview Confirmation",
    ),
)
def test_subject_title_common_wrapper_aliases_terminal_body_event(subject: str) -> None:
    text = f"Subject: {subject}\n\nYour Atlas interview is scheduled for May 14, 2027."
    analysis, plan = analyze_and_plan(text)
    page_plan = plan_gmail_temporal_candidate_pages(
        analysis=analysis,
        batch=plan.batches[0],
    )
    title = next(
        item
        for item in analysis.mentions
        if item.mention_type == "event_title_candidate"
    )
    body_event = next(
        item
        for item in analysis.mentions
        if item.mention_type == "event" and item.field == "body"
    )

    assert any(
        {title.mention_id, body_event.mention_id}.issubset(cluster.subject_mention_ids)
        for page in page_plan.pages
        for cluster in page.clusters
    )


def test_subject_title_wrapper_does_not_alias_conflicting_body_modifier() -> None:
    text = (
        "Subject: Atlas Interview Update\n\n"
        "The Beta interview is scheduled for May 14, 2027."
    )
    analysis, plan = analyze_and_plan(text)
    page_plan = plan_gmail_temporal_candidate_pages(
        analysis=analysis,
        batch=plan.batches[0],
    )
    title = next(
        item
        for item in analysis.mentions
        if item.mention_type == "event_title_candidate"
    )
    body_event = next(
        item
        for item in analysis.mentions
        if item.mention_type == "event" and item.field == "body"
    )

    assert not any(
        {title.mention_id, body_event.mention_id}.issubset(cluster.subject_mention_ids)
        for page in page_plan.pages
        for cluster in page.clusters
    )


def test_page_plan_does_not_cluster_unrecognized_adjacent_event_nouns() -> None:
    text = "The meeting workshop is scheduled for May 14, 2027."
    analysis, plan = analyze_and_plan(text)
    page_plan = plan_gmail_temporal_candidate_pages(
        analysis=analysis,
        batch=plan.batches[0],
    )
    event_ids = {
        item.mention_id
        for item in analysis.mentions
        if item.mention_type == "event"
        and text[item.start : item.end].casefold() in {"meeting", "workshop"}
    }

    assert len(event_ids) == 2
    assert not any(
        event_ids.issubset(cluster.subject_mention_ids)
        for page in page_plan.pages
        for cluster in page.clusters
    )


@pytest.mark.parametrize(
    ("text", "expected_lifecycle"),
    (
        ("The meeting and workshop are scheduled May 14, 2027.", "scheduled"),
        ("The meeting and workshop were cancelled May 14, 2027.", "cancelled"),
    ),
)
def test_frontier_preserves_both_source_verified_coordinated_subjects(
    text: str,
    expected_lifecycle: str,
) -> None:
    analysis, plan = analyze_and_plan(text)
    frontier = build_gmail_temporal_candidate_frontier(
        analysis=analysis,
        batch=plan.batches[0],
    )
    subject_ids = {
        item.mention_id for item in analysis.mentions if item.mention_type == "event"
    }
    lifecycle_candidates = tuple(
        item
        for item in frontier.candidates
        if item.lifecycle == expected_lifecycle
        and item.subject_mention_id in subject_ids
    )

    assert {item.subject_mention_id for item in lifecycle_candidates} == subject_ids
    assert all(item.requires_defer is False for item in lifecycle_candidates)


def test_frontier_does_not_carry_lifecycle_backward_across_clause_subject() -> None:
    text = "The meeting was scheduled and the workshop is May 14, 2027."
    analysis, plan = analyze_and_plan(text)
    frontier = build_gmail_temporal_candidate_frontier(
        analysis=analysis,
        batch=plan.batches[0],
    )
    workshop = next(
        item
        for item in analysis.mentions
        if item.mention_type == "event"
        and text[item.start : item.end].casefold() == "workshop"
    )
    lifecycle_candidate = next(
        item
        for item in frontier.candidates
        if item.subject_mention_id == workshop.mention_id
        and item.lifecycle_mention_id is not None
    )

    assert lifecycle_candidate.lifecycle == "unknown"
    assert lifecycle_candidate.requires_defer is True
    assert "lifecycle_clause_direction_conflict" in lifecycle_candidate.blockers


def test_frontier_prunes_dense_cross_clause_pair_without_losing_true_members() -> None:
    text = (
        "Alpha interview is scheduled for August 14, 2027 at 9:00 AM, "
        "Beta workshop is scheduled for August 16, 2027 at 2:00 PM, "
        "and please submit the board packet by August 18, 2027."
    )
    analysis, plan = analyze_and_plan(text)
    expressions = {item.calendar_date_options[0]: item for item in analysis.expressions}
    mentions = {
        text[item.start : item.end].casefold(): item
        for item in analysis.mentions
        if item.mention_type in {"event", "action"}
    }
    frontiers = tuple(
        build_gmail_temporal_candidate_frontier(analysis=analysis, batch=batch)
        for batch in plan.batches
    )
    candidates = tuple(
        candidate for frontier in frontiers for candidate in frontier.candidates
    )

    assert not any(
        candidate.expression_id
        in {
            expressions["2027-08-14"].expression_id,
            expressions["2027-08-16"].expression_id,
        }
        and candidate.subject_mention_id == mentions["submit"].mention_id
        for candidate in candidates
    )
    assert any(
        candidate.expression_id == expressions["2027-08-14"].expression_id
        and candidate.subject_mention_id == mentions["interview"].mention_id
        and candidate.relation == "occurrence"
        for candidate in candidates
    )
    assert any(
        candidate.expression_id == expressions["2027-08-16"].expression_id
        and candidate.subject_mention_id == mentions["workshop"].mention_id
        and candidate.relation == "occurrence"
        for candidate in candidates
    )
    assert any(
        candidate.expression_id == expressions["2027-08-18"].expression_id
        and candidate.subject_mention_id == mentions["submit"].mention_id
        and candidate.relation == "deadline"
        for candidate in candidates
    )


@pytest.mark.parametrize(
    "text",
    (
        "On August 16, 2027, please submit the packet by August 18, 2027.",
        "The workshop is August 16, 2027, and please submit the packet.",
    ),
)
def test_frontier_keeps_scope_rule_counterexamples(text: str) -> None:
    analysis, plan = analyze_and_plan(text)
    first_expression = analysis.expressions[0]
    submit = next(
        item
        for item in analysis.mentions
        if item.mention_type == "action"
        and text[item.start : item.end].casefold() == "submit"
    )
    first_batch = next(
        batch
        for batch in plan.batches
        if batch.expressions[0].expression_id == first_expression.expression_id
    )
    frontier = build_gmail_temporal_candidate_frontier(
        analysis=analysis,
        batch=first_batch,
    )

    assert any(
        candidate.subject_mention_id == submit.mention_id
        for candidate in frontier.candidates
    )


def test_anaphoric_completion_has_an_independent_frontier_and_cluster() -> None:
    text = (
        "The review meeting took place on August 9, 2027 and was completed "
        "that afternoon."
    )
    analysis, plan = analyze_and_plan(text)
    assert len(analysis.expressions) == 2
    assert len(plan.batches) == 2
    date_expression = next(
        item for item in analysis.expressions if item.form == "explicit_date"
    )
    anaphoric_expression = next(
        item for item in analysis.expressions if item.form == "coarse_relative"
    )
    batch_by_expression = {
        batch.expressions[0].expression_id: batch for batch in plan.batches
    }
    date_batch = batch_by_expression[date_expression.expression_id]
    anaphoric_batch = batch_by_expression[anaphoric_expression.expression_id]
    date_frontier = build_gmail_temporal_candidate_frontier(
        analysis=analysis,
        batch=date_batch,
    )
    anaphoric_frontier = build_gmail_temporal_candidate_frontier(
        analysis=analysis,
        batch=anaphoric_batch,
    )
    meeting = next(
        item
        for item in analysis.mentions
        if item.mention_type == "event"
        and text[item.start : item.end].casefold() == "meeting"
    )

    date_occurrence = next(
        candidate
        for candidate in date_frontier.candidates
        if (
            candidate.subject_mention_id == meeting.mention_id
            and candidate.lifecycle_mention_id is None
            and candidate.relation == "occurrence"
            and candidate.kind == "actual"
            and candidate.lifecycle == "none"
        )
    )
    cross_expression_completion = next(
        candidate
        for candidate in date_frontier.candidates
        if candidate.subject_mention_id == meeting.mention_id
        and candidate.lifecycle_mention_id is not None
    )
    anaphoric_completion = next(
        candidate
        for candidate in anaphoric_frontier.candidates
        if (
            candidate.lifecycle == "completed"
            and candidate.normalized_value is None
            and candidate.requires_defer is True
        )
    )

    date_pages = plan_gmail_temporal_candidate_pages(
        analysis=analysis,
        batch=date_batch,
    )
    anaphoric_pages = plan_gmail_temporal_candidate_pages(
        analysis=analysis,
        batch=anaphoric_batch,
    )
    date_cluster = next(
        cluster
        for page in date_pages.pages
        for cluster in page.clusters
        if date_occurrence.candidate_id in cluster.candidate_ids
    )
    anaphoric_cluster = next(
        cluster
        for page in anaphoric_pages.pages
        for cluster in page.clusters
        if anaphoric_completion.candidate_id in cluster.candidate_ids
    )

    assert date_pages.pages
    assert anaphoric_pages.pages
    assert date_occurrence.requires_defer is False
    assert cross_expression_completion.lifecycle == "unknown"
    assert cross_expression_completion.requires_defer is True
    assert "lifecycle_expression_scope_conflict" in cross_expression_completion.blockers
    assert date_cluster.cluster_id != anaphoric_cluster.cluster_id
    assert date_pages.pages[0].page_fingerprint != (
        anaphoric_pages.pages[0].page_fingerprint
    )


@pytest.mark.parametrize("raw_verdict", ("uncertain", "supported"))
def test_scope_conflicted_completion_review_canonicalizes_to_direct_occurrence(
    raw_verdict: str,
) -> None:
    text = (
        "The review meeting took place on August 9, 2027 and was completed "
        "that afternoon."
    )
    analysis, plan = analyze_and_plan(text)
    date_expression = next(
        item for item in analysis.expressions if item.form == "explicit_date"
    )
    batch = next(
        item
        for item in plan.batches
        if item.expressions[0].expression_id == date_expression.expression_id
    )
    frontier = build_gmail_temporal_candidate_frontier(
        analysis=analysis,
        batch=batch,
    )
    meeting = next(
        item
        for item in analysis.mentions
        if item.mention_type == "event"
        and text[item.start : item.end].casefold() == "meeting"
    )
    occurrence = next(
        candidate
        for candidate in frontier.candidates
        if (
            candidate.subject_mention_id == meeting.mention_id
            and candidate.lifecycle == "none"
            and candidate.relation == "occurrence"
            and candidate.kind == "actual"
        )
    )
    scope_conflicted = next(
        candidate
        for candidate in frontier.candidates
        if candidate.binding_id == occurrence.binding_id
        and candidate.lifecycle == "unknown"
    )
    direct_lead = next(
        item for item in analysis.leads if item.lead_id == occurrence.selected_lead_id
    )
    page_plan = plan_gmail_temporal_candidate_pages(
        analysis=analysis,
        batch=batch,
    )

    assert occurrence.blockers == ()
    assert occurrence.requires_defer is False
    assert direct_lead.association_mode == "direct_grammar"
    assert direct_lead.confidence_tier == "strict_direct"
    assert direct_lead.blockers == ()
    assert {
        "lifecycle_expression_scope_conflict",
        "lifecycle_subject_binding_unverified",
    } <= set(scope_conflicted.blockers)

    result = validate_gmail_temporal_candidate_verdict_set(
        analysis=analysis,
        batch=batch,
        plan=page_plan,
        rows=page_verdict_rows(
            page_plan,
            overrides={scope_conflicted.candidate_id: raw_verdict},
        ),
    )

    assert result.supported_candidate_ids == ()
    assert result.supported_citations == ()
    assert result.unsupported_candidate_count == len(frontier.candidates) - 1
    assert len(result.uncertain_clusters) == 1
    uncertainty = result.uncertain_clusters[0]
    assert uncertainty.plausible_candidate_ids == (occurrence.candidate_id,)
    assert uncertainty.reason == "scope_conflicted_lifecycle_review_canonicalized"
    assert result.requires_defer is True


@pytest.mark.parametrize(
    "near_miss",
    (
        "missing_expression_scope_blocker",
        "missing_subject_binding_blocker",
        "nonactual_kind",
        "nonoccurrence_relation",
        "nonstrict_lead",
        "nondirect_lead",
        "different_binding",
        "different_expression",
        "different_normalized_value",
        "blocked_occurrence",
        "multiple_accepted_candidates",
        "scheduled",
        "cancelled",
        "completed",
    ),
)
def test_scope_conflicted_lifecycle_review_canonicalization_refuses_near_misses(
    near_miss: str,
) -> None:
    text = (
        "The review meeting took place on August 9, 2027 and was completed "
        "that afternoon."
    )
    analysis, plan = analyze_and_plan(text)
    date_expression = next(
        item for item in analysis.expressions if item.form == "explicit_date"
    )
    batch = next(
        item
        for item in plan.batches
        if item.expressions[0].expression_id == date_expression.expression_id
    )
    frontier = build_gmail_temporal_candidate_frontier(
        analysis=analysis,
        batch=batch,
    )
    occurrence = next(
        candidate
        for candidate in frontier.candidates
        if not candidate.blockers
        and candidate.lifecycle == "none"
        and candidate.relation == "occurrence"
        and candidate.kind == "actual"
    )
    scope_conflicted = next(
        candidate
        for candidate in frontier.candidates
        if candidate.binding_id == occurrence.binding_id
        and candidate.lifecycle == "unknown"
    )
    page_plan = plan_gmail_temporal_candidate_pages(
        analysis=analysis,
        batch=batch,
    )
    ((_, cluster_candidate_ids),) = parent_cluster_candidate_ids(page_plan)
    candidates = {
        candidate.candidate_id: candidate for candidate in frontier.candidates
    }
    leads = {lead.lead_id: lead for lead in analysis.leads}
    verdicts = {candidate_id: "unsupported" for candidate_id in candidates}
    verdicts[scope_conflicted.candidate_id] = "uncertain"

    if near_miss == "missing_expression_scope_blocker":
        candidates[scope_conflicted.candidate_id] = replace(
            scope_conflicted,
            blockers=tuple(
                blocker
                for blocker in scope_conflicted.blockers
                if blocker != "lifecycle_expression_scope_conflict"
            ),
        )
    elif near_miss == "missing_subject_binding_blocker":
        candidates[scope_conflicted.candidate_id] = replace(
            scope_conflicted,
            blockers=tuple(
                blocker
                for blocker in scope_conflicted.blockers
                if blocker != "lifecycle_subject_binding_unverified"
            ),
        )
    elif near_miss == "nonactual_kind":
        candidates[occurrence.candidate_id] = replace(occurrence, kind="planned")
    elif near_miss == "nonoccurrence_relation":
        candidates[occurrence.candidate_id] = replace(
            occurrence,
            relation="deadline",
        )
    elif near_miss == "nonstrict_lead":
        leads[occurrence.selected_lead_id] = replace(
            leads[occurrence.selected_lead_id],
            confidence_tier="review_ambiguous",
        )
    elif near_miss == "nondirect_lead":
        leads[occurrence.selected_lead_id] = replace(
            leads[occurrence.selected_lead_id],
            association_mode="field_local",
        )
    elif near_miss == "different_binding":
        candidates[occurrence.candidate_id] = replace(
            occurrence,
            binding_id=occurrence.binding_id + "-other",
        )
    elif near_miss == "different_expression":
        candidates[occurrence.candidate_id] = replace(
            occurrence,
            expression_id=occurrence.expression_id + "-other",
        )
    elif near_miss == "different_normalized_value":
        candidates[occurrence.candidate_id] = replace(
            occurrence,
            normalized_value="2027-08-10",
        )
    elif near_miss == "blocked_occurrence":
        candidates[occurrence.candidate_id] = replace(
            occurrence,
            blockers=("multiple_association_expressions",),
        )
    elif near_miss == "multiple_accepted_candidates":
        verdicts[occurrence.candidate_id] = "supported"
    else:
        candidates[scope_conflicted.candidate_id] = replace(
            scope_conflicted,
            lifecycle=near_miss,
        )

    assert (
        _canonicalize_scope_conflicted_lifecycle_review(
            cluster_candidate_ids=cluster_candidate_ids,
            candidates=candidates,
            leads=leads,
            verdicts=verdicts,
        )
        is None
    )


def test_scope_conflicted_lifecycle_review_does_not_override_multiple_accepts() -> None:
    text = (
        "The review meeting took place on August 9, 2027 and was completed "
        "that afternoon."
    )
    analysis, plan = analyze_and_plan(text)
    date_expression = next(
        item for item in analysis.expressions if item.form == "explicit_date"
    )
    batch = next(
        item
        for item in plan.batches
        if item.expressions[0].expression_id == date_expression.expression_id
    )
    frontier = build_gmail_temporal_candidate_frontier(
        analysis=analysis,
        batch=batch,
    )
    occurrence = next(
        candidate
        for candidate in frontier.candidates
        if not candidate.blockers
        and candidate.lifecycle == "none"
        and candidate.kind == "actual"
    )
    scope_conflicted = next(
        candidate
        for candidate in frontier.candidates
        if candidate.binding_id == occurrence.binding_id
        and candidate.lifecycle == "unknown"
    )
    page_plan = plan_gmail_temporal_candidate_pages(
        analysis=analysis,
        batch=batch,
    )

    result = validate_gmail_temporal_candidate_verdict_set(
        analysis=analysis,
        batch=batch,
        plan=page_plan,
        rows=page_verdict_rows(
            page_plan,
            overrides={
                occurrence.candidate_id: "supported",
                scope_conflicted.candidate_id: "uncertain",
            },
        ),
    )

    assert result.supported_candidate_ids == ()
    assert result.uncertain_clusters[0].plausible_candidate_ids == (
        occurrence.candidate_id,
        scope_conflicted.candidate_id,
    )
    assert result.uncertain_clusters[0].reason == "model_uncertain"


@pytest.mark.parametrize(
    "text",
    (
        "The meeting was cancelled on August 14, 2027 after being scheduled for "
        "August 10, 2027.",
        "The meeting on August 14, 2027 was cancelled after being scheduled for "
        "August 10, 2027.",
    ),
)
def test_frontier_prunes_crossed_lifecycle_cues_and_keeps_local_bindings(
    text: str,
) -> None:
    analysis, plan = analyze_and_plan(text)
    expressions = {item.calendar_date_options[0]: item for item in analysis.expressions}
    lifecycles = {
        item.lifecycle_role: item
        for item in analysis.mentions
        if item.mention_type == "lifecycle"
    }
    candidates = tuple(
        candidate
        for batch in plan.batches
        for candidate in build_gmail_temporal_candidate_frontier(
            analysis=analysis,
            batch=batch,
        ).candidates
    )

    assert len(candidates) == 2
    assert {item.lifecycle for item in candidates} == {"cancelled", "scheduled"}

    cancellation = next(
        candidate
        for candidate in candidates
        if candidate.expression_id == expressions["2027-08-14"].expression_id
        and candidate.lifecycle_mention_id == lifecycles["cancelled"].mention_id
    )
    scheduled = next(
        candidate
        for candidate in candidates
        if candidate.expression_id == expressions["2027-08-10"].expression_id
        and candidate.lifecycle_mention_id == lifecycles["scheduled"].mention_id
    )

    assert cancellation.lifecycle == "cancelled"
    assert cancellation.normalized_value == "2027-08-14"
    assert cancellation.requires_defer is False
    assert scheduled.lifecycle == "scheduled"
    assert scheduled.normalized_value == "2027-08-10"
    assert scheduled.requires_defer is False
    assert not any(
        candidate.expression_id == expressions["2027-08-10"].expression_id
        and candidate.lifecycle_mention_id == lifecycles["cancelled"].mention_id
        for candidate in candidates
    )
    assert not any(
        candidate.expression_id == expressions["2027-08-14"].expression_id
        and candidate.lifecycle_mention_id == lifecycles["scheduled"].mention_id
        for candidate in candidates
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
    analysis, plan = analyze_and_plan(f"The {private_marker} meeting is May 14, 2027.")
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
        assert (
            len(
                gmail_temporal_candidate_page_payload(
                    frontier=frontier,
                    page=page,
                ).encode("utf-8")
            )
            <= 5_500
        )


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
        "The interview is scheduled for May 14, 2027 at 4:30 PM."
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
    analysis, plan = analyze_and_plan("The interview is scheduled for May 14, 2027.")
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
    assert uncertainty.plausible_candidate_ids == (page_plan.covered_candidate_ids[0],)
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
    candidate_id = next(
        candidate.candidate_id
        for candidate in build_gmail_temporal_candidate_frontier(
            analysis=analysis,
            batch=batch,
        ).candidates
        if candidate.lifecycle == "scheduled"
    )

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


def test_supported_deferred_schedule_base_becomes_lifecycle_uncertainty() -> None:
    analysis, plan = analyze_and_plan(
        "The workshop is scheduled for May 14, 2027 at 2:00 PM."
    )
    batch = plan.batches[0]
    page_plan = plan_gmail_temporal_candidate_pages(
        analysis=analysis,
        batch=batch,
    )
    frontier = build_gmail_temporal_candidate_frontier(
        analysis=analysis,
        batch=batch,
    )
    base = next(
        candidate
        for candidate in frontier.candidates
        if candidate.lifecycle_mention_id is None
    )
    scheduled = next(
        candidate
        for candidate in frontier.candidates
        if candidate.lifecycle == "scheduled"
    )
    cluster_by_candidate = {
        candidate_id: cluster_id
        for cluster_id, candidate_ids in parent_cluster_candidate_ids(page_plan)
        for candidate_id in candidate_ids
    }
    assert (
        cluster_by_candidate[scheduled.candidate_id]
        == cluster_by_candidate[base.candidate_id]
    )

    result = validate_gmail_temporal_candidate_verdict_set(
        analysis=analysis,
        batch=batch,
        plan=page_plan,
        rows=page_verdict_rows(
            page_plan,
            overrides={base.candidate_id: "supported"},
        ),
    )

    assert scheduled.requires_defer is True
    assert scheduled.normalized_value is None
    assert result.supported_candidate_ids == ()
    assert result.supported_citations == ()
    assert result.unsupported_candidate_count == 1
    assert len(result.uncertain_clusters) == 1
    uncertainty = result.uncertain_clusters[0]
    assert uncertainty.plausible_candidate_ids == (base.candidate_id,)
    assert uncertainty.reason == "lifecycle_refinement_unresolved"
    assert result.requires_defer is True


def test_supported_deferred_schedule_lifecycle_remains_exactly_supported() -> None:
    analysis, plan = analyze_and_plan(
        "The workshop is scheduled for May 14, 2027 at 2:00 PM."
    )
    batch = plan.batches[0]
    page_plan = plan_gmail_temporal_candidate_pages(
        analysis=analysis,
        batch=batch,
    )
    scheduled = next(
        candidate
        for candidate in build_gmail_temporal_candidate_frontier(
            analysis=analysis,
            batch=batch,
        ).candidates
        if candidate.lifecycle == "scheduled"
    )

    result = validate_gmail_temporal_candidate_verdict_set(
        analysis=analysis,
        batch=batch,
        plan=page_plan,
        rows=page_verdict_rows(
            page_plan,
            overrides={scheduled.candidate_id: "supported"},
        ),
    )

    assert result.supported_candidate_ids == (scheduled.candidate_id,)
    assert len(result.supported_citations) == 1
    assert result.uncertain_clusters == ()
    assert result.requires_defer is True


@pytest.mark.parametrize("lifecycle", ("cancelled", "completed"))
def test_supported_deferred_terminal_base_becomes_lifecycle_uncertainty(
    lifecycle: str,
) -> None:
    analysis, plan = analyze_and_plan(
        f"The workshop was {lifecycle} on May 14, 2027 at 2:00 PM."
    )
    batch = plan.batches[0]
    page_plan = plan_gmail_temporal_candidate_pages(
        analysis=analysis,
        batch=batch,
    )
    frontier = build_gmail_temporal_candidate_frontier(
        analysis=analysis,
        batch=batch,
    )
    base = next(
        candidate
        for candidate in frontier.candidates
        if candidate.lifecycle_mention_id is None
    )

    result = validate_gmail_temporal_candidate_verdict_set(
        analysis=analysis,
        batch=batch,
        plan=page_plan,
        rows=page_verdict_rows(
            page_plan,
            overrides={base.candidate_id: "supported"},
        ),
    )

    assert result.supported_candidate_ids == ()
    assert result.uncertain_clusters[0].plausible_candidate_ids == (base.candidate_id,)
    assert result.uncertain_clusters[0].reason == "lifecycle_refinement_unresolved"


def test_supported_direct_actual_is_not_shadowed_by_deferred_completion() -> None:
    analysis, plan = analyze_and_plan(
        "The workshop occurred on May 14, 2027 at 2:00 PM and was completed."
    )
    batch = plan.batches[0]
    page_plan = plan_gmail_temporal_candidate_pages(
        analysis=analysis,
        batch=batch,
    )
    frontier = build_gmail_temporal_candidate_frontier(
        analysis=analysis,
        batch=batch,
    )
    actual = next(
        candidate
        for candidate in frontier.candidates
        if (candidate.relation, candidate.kind, candidate.lifecycle)
        == ("occurrence", "actual", "none")
    )
    completed = next(
        candidate
        for candidate in frontier.candidates
        if candidate.lifecycle == "completed"
    )
    direct_lead = next(
        lead for lead in analysis.leads if lead.lead_id == actual.selected_lead_id
    )

    assert direct_lead.association_mode == "direct_grammar"
    assert direct_lead.confidence_tier == "review_ambiguous"
    assert _is_direct_actual_endpoint(
        base=actual,
        leads={direct_lead.lead_id: direct_lead},
    )
    assert _explicit_lifecycle_subsumes_base(
        lifecycle=completed,
        base=actual,
        leads={direct_lead.lead_id: direct_lead},
    )
    assert not _is_direct_actual_endpoint(
        base=actual,
        leads={
            direct_lead.lead_id: replace(
                direct_lead,
                association_mode="field_local",
            )
        },
    )

    result = validate_gmail_temporal_candidate_verdict_set(
        analysis=analysis,
        batch=batch,
        plan=page_plan,
        rows=page_verdict_rows(
            page_plan,
            overrides={actual.candidate_id: "supported"},
        ),
    )

    assert result.supported_candidate_ids == (actual.candidate_id,)
    assert result.uncertain_clusters == ()


def test_supported_reschedule_base_becomes_lifecycle_uncertainty() -> None:
    analysis, plan = analyze_and_plan(
        "The workshop was rescheduled from May 14, 2027 at 2:00 PM "
        "to May 16, 2027 at 3:00 PM."
    )

    for batch in plan.batches:
        page_plan = plan_gmail_temporal_candidate_pages(
            analysis=analysis,
            batch=batch,
        )
        frontier = build_gmail_temporal_candidate_frontier(
            analysis=analysis,
            batch=batch,
        )
        base = next(
            candidate
            for candidate in frontier.candidates
            if candidate.lifecycle_mention_id is None
        )
        unknown = next(
            candidate
            for candidate in frontier.candidates
            if candidate.lifecycle == "unknown"
        )

        result = validate_gmail_temporal_candidate_verdict_set(
            analysis=analysis,
            batch=batch,
            plan=page_plan,
            rows=page_verdict_rows(
                page_plan,
                overrides={base.candidate_id: "supported"},
            ),
        )

        assert "rescheduled_endpoint_role_unresolved" in unknown.blockers
        assert result.supported_candidate_ids == ()
        assert result.uncertain_clusters[0].plausible_candidate_ids == (
            base.candidate_id,
        )
        assert result.uncertain_clusters[0].reason == "lifecycle_refinement_unresolved"


@pytest.mark.parametrize("tail", (" or 17", "/17"))
def test_abbreviated_reschedule_day_candidates_are_forced_to_defer(
    tail: str,
) -> None:
    text = f"The workshop was rescheduled from May 14, 2027 to May 16, 2027{tail}."
    analysis, plan = analyze_and_plan(text)
    shorthand = next(
        item
        for item in analysis.expressions
        if item.form == "abbreviated_shared_month_day"
    )
    candidates = tuple(
        candidate
        for batch in plan.batches
        for candidate in build_gmail_temporal_candidate_frontier(
            analysis=analysis,
            batch=batch,
        ).candidates
        if candidate.expression_id == shorthand.expression_id
    )

    assert candidates
    assert {item.normalized_value for item in candidates} == {"2027-05-17"}
    assert all(item.requires_defer is True for item in candidates)
    assert all(
        "reschedule_endpoint_alternatives_unresolved" in item.blockers
        for item in candidates
    )


def test_three_run_ensemble_preserves_only_consensus_candidates() -> None:
    analysis, plan = analyze_and_plan(
        "The workshop is scheduled for May 14, 2027 at 2:00 PM."
    )
    batch = plan.batches[0]
    page_plan = plan_gmail_temporal_candidate_pages(
        analysis=analysis,
        batch=batch,
    )
    frontier = build_gmail_temporal_candidate_frontier(
        analysis=analysis,
        batch=batch,
    )
    base = next(
        candidate
        for candidate in frontier.candidates
        if candidate.lifecycle_mention_id is None
    )
    scheduled = next(
        candidate
        for candidate in frontier.candidates
        if candidate.lifecycle == "scheduled"
    )
    runs = (
        page_verdict_rows(
            page_plan,
            overrides={scheduled.candidate_id: "supported"},
        ),
        page_verdict_rows(
            page_plan,
            overrides={scheduled.candidate_id: "supported"},
        ),
        page_verdict_rows(
            page_plan,
            overrides={base.candidate_id: "supported"},
        ),
    )

    result = validate_gmail_temporal_candidate_ensemble_verdict_set(
        analysis=analysis,
        batch=batch,
        plan=page_plan,
        runs=runs,
    )
    consensus = {
        verdict.candidate_id: verdict.verdict
        for row in result.consensus_rows
        for verdict in row.verdicts
    }

    assert isinstance(result, GmailTemporalCandidateEnsembleVerdictSet)
    assert result.version == "gmail_temporal_candidate_three_run_ensemble_v3"
    assert result.policy_version == "gmail_temporal_candidate_three_run_consensus_v3"
    assert result.run_count == 3
    assert result.policy_fingerprint == (
        gmail_temporal_candidate_ensemble_policy_fingerprint()
    )
    assert result.policy_fingerprint.startswith("gtfep_")
    assert consensus[scheduled.candidate_id] == "uncertain"
    assert consensus[base.candidate_id] == "unsupported"
    assert result.verdict_set.supported_candidate_ids == ()
    assert result.verdict_set.uncertain_clusters[0].plausible_candidate_ids == (
        scheduled.candidate_id,
    )
    assert result.cluster_reviews == ()


def test_three_run_ensemble_retains_cross_run_alias_switching_as_uncertain() -> None:
    analysis, plan = analyze_and_plan(
        "The review meeting is scheduled for May 14, 2027 at 2:00 PM."
    )
    batch = plan.batches[0]
    page_plan = plan_gmail_temporal_candidate_pages(
        analysis=analysis,
        batch=batch,
    )
    frontier = build_gmail_temporal_candidate_frontier(
        analysis=analysis,
        batch=batch,
    )
    aliases = tuple(
        candidate
        for candidate in frontier.candidates
        if candidate.lifecycle_mention_id is None
    )
    assert len(aliases) == 2
    assert len({candidate.subject_mention_id for candidate in aliases}) == 2
    cluster_by_candidate = {
        candidate_id: cluster_id
        for cluster_id, candidate_ids in parent_cluster_candidate_ids(page_plan)
        for candidate_id in candidate_ids
    }
    assert (
        len({cluster_by_candidate[candidate.candidate_id] for candidate in aliases})
        == 1
    )

    runs = (
        page_verdict_rows(
            page_plan,
            overrides={aliases[0].candidate_id: "supported"},
        ),
        page_verdict_rows(
            page_plan,
            overrides={aliases[1].candidate_id: "supported"},
        ),
        page_verdict_rows(page_plan),
    )
    result = validate_gmail_temporal_candidate_ensemble_verdict_set(
        analysis=analysis,
        batch=batch,
        plan=page_plan,
        runs=runs,
    )
    consensus = {
        verdict.candidate_id: verdict.verdict
        for row in result.consensus_rows
        for verdict in row.verdicts
    }
    alias_ids = tuple(candidate.candidate_id for candidate in aliases)
    alias_id_set = set(alias_ids)
    canonical_alias_ids = tuple(
        candidate_id
        for _cluster_id, candidate_ids in parent_cluster_candidate_ids(page_plan)
        for candidate_id in candidate_ids
        if candidate_id in alias_id_set
    )

    canonical_alias_id = canonical_alias_ids[0]
    assert consensus[canonical_alias_id] == "uncertain"
    assert all(
        verdict == "unsupported"
        for candidate_id, verdict in consensus.items()
        if candidate_id != canonical_alias_id
    )
    assert result.verdict_set.supported_candidate_ids == ()
    assert len(result.verdict_set.uncertain_clusters) == 1
    uncertainty = result.verdict_set.uncertain_clusters[0]
    assert uncertainty.plausible_candidate_ids == (canonical_alias_id,)
    assert uncertainty.reason == "model_uncertain"
    assert uncertainty.routable is False
    assert result.cluster_reviews == ()


def test_three_run_ensemble_does_not_admit_one_vote_different_lifecycle() -> None:
    analysis, plan = analyze_and_plan(
        "The review meeting is scheduled for May 14, 2027 at 2:00 PM."
    )
    batch = plan.batches[0]
    page_plan = plan_gmail_temporal_candidate_pages(
        analysis=analysis,
        batch=batch,
    )
    frontier = build_gmail_temporal_candidate_frontier(
        analysis=analysis,
        batch=batch,
    )
    aliases = tuple(
        candidate
        for candidate in frontier.candidates
        if candidate.lifecycle_mention_id is None
    )
    scheduled = next(
        candidate
        for candidate in frontier.candidates
        if candidate.lifecycle == "scheduled"
    )
    alias_ids = {candidate.candidate_id for candidate in aliases}
    canonical_alias_id = next(
        candidate_id
        for _cluster_id, candidate_ids in parent_cluster_candidate_ids(page_plan)
        for candidate_id in candidate_ids
        if candidate_id in alias_ids
    )

    result = validate_gmail_temporal_candidate_ensemble_verdict_set(
        analysis=analysis,
        batch=batch,
        plan=page_plan,
        runs=(
            page_verdict_rows(
                page_plan,
                overrides={aliases[0].candidate_id: "supported"},
            ),
            page_verdict_rows(
                page_plan,
                overrides={aliases[1].candidate_id: "supported"},
            ),
            page_verdict_rows(
                page_plan,
                overrides={scheduled.candidate_id: "supported"},
            ),
        ),
    )
    consensus = {
        verdict.candidate_id: verdict.verdict
        for row in result.consensus_rows
        for verdict in row.verdicts
    }

    assert consensus[canonical_alias_id] == "uncertain"
    assert consensus[scheduled.candidate_id] == "unsupported"
    assert all(
        verdict == "unsupported"
        for candidate_id, verdict in consensus.items()
        if candidate_id != canonical_alias_id
    )
    assert result.verdict_set.uncertain_clusters[0].plausible_candidate_ids == (
        canonical_alias_id,
    )
    assert result.cluster_reviews == ()


def test_three_run_ensemble_routes_split_semantics_to_cluster_review_only() -> None:
    analysis, plan = analyze_and_plan(
        "The review meeting is scheduled, then cancelled, for May 14, 2027 at 2:00 PM."
    )
    batch = plan.batches[0]
    page_plan = plan_gmail_temporal_candidate_pages(
        analysis=analysis,
        batch=batch,
    )
    frontier = build_gmail_temporal_candidate_frontier(
        analysis=analysis,
        batch=batch,
    )
    ((cluster_id, cluster_candidate_ids),) = parent_cluster_candidate_ids(page_plan)
    candidates = {item.candidate_id: item for item in frontier.candidates}
    signature_candidates: dict[tuple[object, ...], str] = {}
    for candidate_id in cluster_candidate_ids:
        candidate = candidates[candidate_id]
        signature_candidates.setdefault(
            (
                candidate.expression_id,
                candidate.relation,
                candidate.kind,
                candidate.lifecycle,
                candidate.normalized_value,
            ),
            candidate_id,
        )
    assert len(signature_candidates) >= 3
    three_semantics = tuple(signature_candidates.values())[:3]

    result = validate_gmail_temporal_candidate_ensemble_verdict_set(
        analysis=analysis,
        batch=batch,
        plan=page_plan,
        runs=tuple(
            page_verdict_rows(
                page_plan,
                overrides={candidate_id: "supported"},
            )
            for candidate_id in three_semantics
        ),
    )
    consensus = {
        verdict.candidate_id: verdict.verdict
        for row in result.consensus_rows
        for verdict in row.verdicts
    }

    assert set(consensus.values()) == {"unsupported"}
    assert result.verdict_set.supported_candidate_ids == ()
    assert result.verdict_set.uncertain_clusters == ()
    assert result.verdict_set.unsupported_candidate_count == len(frontier.candidates)
    assert len(result.cluster_reviews) == 1
    review = result.cluster_reviews[0]
    assert review.version == "gmail_temporal_candidate_ensemble_cluster_review_v1"
    assert review.cluster_id == cluster_id
    assert review.reason == "split_semantics_unresolved"
    assert review.requires_defer is True
    assert review.routable is False


@pytest.mark.parametrize(
    ("votes", "expected"),
    (
        (("supported", "supported", "supported"), "supported"),
        (("supported", "supported", "unsupported"), "uncertain"),
        (("supported", "uncertain", "unsupported"), "uncertain"),
        (("uncertain", "uncertain", "uncertain"), "uncertain"),
        (("supported", "unsupported", "unsupported"), "unsupported"),
    ),
)
def test_three_run_ensemble_vote_policy(
    votes: tuple[str, str, str],
    expected: str,
) -> None:
    analysis, plan = analyze_and_plan("The meeting is May 14, 2027.")
    batch = plan.batches[0]
    page_plan = plan_gmail_temporal_candidate_pages(
        analysis=analysis,
        batch=batch,
    )
    candidate_id = page_plan.covered_candidate_ids[0]
    runs = tuple(
        page_verdict_rows(
            page_plan,
            overrides={candidate_id: vote},
        )
        for vote in votes
    )

    result = validate_gmail_temporal_candidate_ensemble_verdict_set(
        analysis=analysis,
        batch=batch,
        plan=page_plan,
        runs=runs,
    )
    consensus = result.consensus_rows[0].verdicts[0]

    assert consensus.candidate_id == candidate_id
    assert consensus.verdict == expected


def test_three_run_ensemble_applies_lifecycle_calibration_after_voting() -> None:
    analysis, plan = analyze_and_plan(
        "The workshop is scheduled for May 14, 2027 at 2:00 PM."
    )
    batch = plan.batches[0]
    page_plan = plan_gmail_temporal_candidate_pages(
        analysis=analysis,
        batch=batch,
    )
    base = next(
        candidate
        for candidate in build_gmail_temporal_candidate_frontier(
            analysis=analysis,
            batch=batch,
        ).candidates
        if candidate.lifecycle_mention_id is None
    )
    run = page_verdict_rows(
        page_plan,
        overrides={base.candidate_id: "supported"},
    )

    result = validate_gmail_temporal_candidate_ensemble_verdict_set(
        analysis=analysis,
        batch=batch,
        plan=page_plan,
        runs=(run, run, run),
    )

    assert (
        next(
            verdict.verdict
            for row in result.consensus_rows
            for verdict in row.verdicts
            if verdict.candidate_id == base.candidate_id
        )
        == "supported"
    )
    assert result.verdict_set.supported_candidate_ids == ()
    assert result.verdict_set.uncertain_clusters[0].plausible_candidate_ids == (
        base.candidate_id,
    )
    assert (
        result.verdict_set.uncertain_clusters[0].reason
        == "lifecycle_refinement_unresolved"
    )


def test_three_run_ensemble_rejects_wrong_count_and_incomplete_run() -> None:
    analysis, plan = analyze_and_plan("The meeting is May 14, 2027.")
    batch = plan.batches[0]
    page_plan = plan_gmail_temporal_candidate_pages(
        analysis=analysis,
        batch=batch,
    )
    run = page_verdict_rows(page_plan)

    with pytest.raises(GmailTemporalFrontierError, match="exactly three"):
        validate_gmail_temporal_candidate_ensemble_verdict_set(
            analysis=analysis,
            batch=batch,
            plan=page_plan,
            runs=(run, run),
        )

    with pytest.raises(GmailTemporalFrontierError, match="cover the page plan"):
        validate_gmail_temporal_candidate_ensemble_verdict_set(
            analysis=analysis,
            batch=batch,
            plan=page_plan,
            runs=(run, run, run[:-1]),
        )


def test_supported_plus_uncertain_cluster_becomes_model_uncertainty() -> None:
    analysis, plan = analyze_and_plan(
        "The interview is scheduled for May 14, 2027 at 4:30 PM."
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
        "The interview is scheduled for May 14, 2027 at 4:30 PM."
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
    assert result.uncertain_clusters[0].reason == "conflicting_supported_candidates"
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
        item for item in clusters if item[0] in alias_cluster_ids and len(item[1]) >= 3
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
        replace(row, verdicts=tuple(reversed(row.verdicts))) for row in reversed(rows)
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
        "The interview is scheduled for May 14, 2027 at 4:30 PM."
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


@pytest.mark.parametrize(
    ("body", "expected_lifecycle", "expected_normalized"),
    (
        (
            "Nimbus Interview is scheduled for May 14, 2027 at 16:30 -07:00.",
            "scheduled",
            "2027-05-14T16:30:00-07:00",
        ),
        (
            "Nimbus Interview was cancelled on May 14, 2027.",
            "cancelled",
            "2027-05-14",
        ),
        (
            "Nimbus Interview was completed on May 14, 2027.",
            "completed",
            "2027-05-14",
        ),
    ),
)
def test_exact_lifecycle_subsumes_source_verified_alias_bases(
    body: str,
    expected_lifecycle: str,
    expected_normalized: str,
) -> None:
    analysis, plan = analyze_and_plan(f"Subject: Nimbus Interview\n\n{body}")
    frontier = build_gmail_temporal_candidate_frontier(
        analysis=analysis,
        batch=plan.batches[0],
    )

    exact = tuple(
        candidate
        for candidate in frontier.candidates
        if candidate.lifecycle == expected_lifecycle and not candidate.requires_defer
    )
    unknown = tuple(
        candidate
        for candidate in frontier.candidates
        if candidate.lifecycle == "unknown"
    )

    assert len(exact) == 1
    assert exact[0].normalized_value == expected_normalized
    assert not any(
        candidate.lifecycle_mention_id is None for candidate in frontier.candidates
    )
    assert len(unknown) == 2
    assert all(candidate.requires_defer for candidate in unknown)


def test_deferred_lifecycle_does_not_subsume_alias_bases() -> None:
    analysis, plan = analyze_and_plan(
        "The review meeting is scheduled for May 14, 2027 at 2:00 PM."
    )
    frontier = build_gmail_temporal_candidate_frontier(
        analysis=analysis,
        batch=plan.batches[0],
    )

    lifecycle_free = tuple(
        candidate
        for candidate in frontier.candidates
        if candidate.lifecycle_mention_id is None
    )
    scheduled = tuple(
        candidate
        for candidate in frontier.candidates
        if candidate.lifecycle == "scheduled"
    )

    assert len(lifecycle_free) == 2
    assert len(scheduled) == 2
    assert all(candidate.requires_defer for candidate in scheduled)
    assert all(candidate.normalized_value is None for candidate in scheduled)


def test_reschedule_aliases_keep_unknown_lifecycle_and_free_bases() -> None:
    analysis, plan = analyze_and_plan(
        "The review meeting was rescheduled from May 14, 2027 to May 16, 2027."
    )

    for batch in plan.batches:
        frontier = build_gmail_temporal_candidate_frontier(
            analysis=analysis,
            batch=batch,
        )
        lifecycle_free = tuple(
            candidate
            for candidate in frontier.candidates
            if candidate.lifecycle_mention_id is None
        )
        unknown = tuple(
            candidate
            for candidate in frontier.candidates
            if candidate.lifecycle == "unknown"
        )

        assert len(lifecycle_free) == 2
        assert len(unknown) == 2
        assert all(candidate.requires_defer for candidate in unknown)


def test_terminal_lifecycle_does_not_subsume_distinct_direct_actual() -> None:
    analysis, plan = analyze_and_plan(
        "The review meeting occurred on May 14, 2027 and was completed."
    )
    frontier = build_gmail_temporal_candidate_frontier(
        analysis=analysis,
        batch=plan.batches[0],
    )

    actual = tuple(
        candidate
        for candidate in frontier.candidates
        if (
            candidate.relation,
            candidate.kind,
            candidate.lifecycle,
        )
        == ("occurrence", "actual", "none")
    )
    completed = tuple(
        candidate
        for candidate in frontier.candidates
        if candidate.lifecycle == "completed"
    )

    assert actual
    assert all(not candidate.requires_defer for candidate in actual)
    assert completed


def test_exact_lifecycle_does_not_subsume_source_distinct_title_bases() -> None:
    text = (
        "Subject: Atlas Interview Update\n\n"
        "The Beta interview is scheduled for May 14, 2027."
    )
    analysis, plan = analyze_and_plan(text)
    frontier = build_gmail_temporal_candidate_frontier(
        analysis=analysis,
        batch=plan.batches[0],
    )
    title_ids = {
        mention.mention_id
        for mention in analysis.mentions
        if mention.field == "subject"
        and mention.mention_type in {"event", "event_title_candidate"}
    }

    assert any(
        candidate.subject_mention_id in title_ids
        and candidate.lifecycle_mention_id is None
        for candidate in frontier.candidates
    )
    assert any(
        candidate.lifecycle == "scheduled" and not candidate.requires_defer
        for candidate in frontier.candidates
    )


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
    assert all(
        (item.relation, item.kind) == ("unspecified", "unspecified")
        for item in rescheduled
    )
    assert all(item.requires_defer is True for item in rescheduled)
    lifecycle_free = [
        candidate
        for frontier in frontiers
        for candidate in frontier.candidates
        if candidate.lifecycle_mention_id is None
    ]
    assert len(lifecycle_free) == 2
    assert all(item.lifecycle == "none" for item in lifecycle_free)


def test_unrelated_lifecycle_cue_is_not_crossed_into_other_segment() -> None:
    analysis, plan = analyze_and_plan(
        "The meeting is scheduled for May 14, 2027. The workshop was cancelled."
    )
    frontier = build_gmail_temporal_candidate_frontier(
        analysis=analysis,
        batch=plan.batches[0],
    )

    assert {item.lifecycle for item in frontier.candidates} == {"scheduled"}


@pytest.mark.parametrize("value", (0, -1, True, 1.5))
def test_page_plan_rejects_invalid_cluster_cap(value: object) -> None:
    analysis, plan = analyze_and_plan("The meeting is May 14, 2027.")

    with pytest.raises(ValueError, match="positive integer"):
        plan_gmail_temporal_candidate_pages(
            analysis=analysis,
            batch=plan.batches[0],
            max_clusters_per_page=value,  # type: ignore[arg-type]
        )
