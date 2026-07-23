from __future__ import annotations

import pytest

from pkm_brain.gmail_temporal_batching import plan_gmail_temporal_selector_batches
from pkm_brain.gmail_temporal_frontier import (
    build_gmail_temporal_candidate_frontier,
)
from pkm_brain.gmail_temporal_leads import analyze_gmail_temporal_leads


ANCHOR = "2027-09-18T08:00:00-07:00"


def analyze(text: str):
    return analyze_gmail_temporal_leads(
        text=text,
        message_internal_at=ANCHOR,
        fact_admitted=True,
        chunk_id="synthetic-event-subject-recall",
    )


def source_bound_titles(text: str):
    analysis = analyze(text)
    titles = tuple(
        item
        for item in analysis.mentions
        if item.mention_type == "event_title_candidate"
        and "clause_bound_event_title_review_only" in item.blockers
    )
    return analysis, titles


@pytest.mark.parametrize(
    ("text", "expected_title", "expected_dates"),
    (
        (
            "The Lumen Quay planning session has been rescheduled to "
            "September 22, 2027 from September 19, 2027.",
            "Lumen Quay planning session",
            {"September 22, 2027", "September 19, 2027"},
        ),
        (
            "The Northstar design review scheduled for October 6, 2027 "
            "has been cancelled.",
            "Northstar design review",
            {"October 6, 2027"},
        ),
        (
            "The Marigold project debrief may happen on September 25, 2027 "
            "or September 26, 2027.",
            "Marigold project debrief",
            {"September 25, 2027", "September 26, 2027"},
        ),
        (
            "The eBay partner summit scheduled for October 7, 2027.",
            "eBay partner summit",
            {"October 7, 2027"},
        ),
        (
            "The Lumen of the Sea conference scheduled for October 8, 2027.",
            "Lumen of the Sea conference",
            {"October 8, 2027"},
        ),
    ),
)
def test_source_bound_event_clause_recovers_full_review_only_identity(
    text: str,
    expected_title: str,
    expected_dates: set[str],
) -> None:
    analysis, titles = source_bound_titles(text)

    assert len(titles) == 1
    title = titles[0]
    assert text[title.start : title.end] == expected_title
    assert title.relation == "occurrence"
    assert title.kind is None
    assert title.blockers == (
        "event_title_review_only",
        "clause_bound_event_title_review_only",
    )

    expression_surfaces = {
        expression.expression_id: text[expression.start : expression.end]
        for expression in analysis.expressions
    }
    title_leads = tuple(
        lead for lead in analysis.leads if lead.mention_id == title.mention_id
    )
    assert {expression_surfaces[lead.expression_id] for lead in title_leads} == (
        expected_dates
    )
    assert all(lead.association_mode == "field_local" for lead in title_leads)
    assert all(
        next(
            item
            for item in analysis.expressions
            if item.expression_id == lead.expression_id
        ).segment_id
        == title.segment_id
        for lead in title_leads
    )


def test_alternative_dates_reach_frontier_but_confirmation_date_stays_an_action() -> (
    None
):
    text = (
        "Subject: Marigold debrief options\n\n"
        "The Marigold project debrief may happen on September 25, 2027 or "
        "September 26, 2027. I will confirm the final date tomorrow."
    )
    analysis, titles = source_bound_titles(text)
    assert len(titles) == 1
    title = titles[0]

    expressions = {text[item.start : item.end]: item for item in analysis.expressions}
    title_expression_ids = {
        lead.expression_id
        for lead in analysis.leads
        if lead.mention_id == title.mention_id
    }
    assert title_expression_ids == {
        expressions["September 25, 2027"].expression_id,
        expressions["September 26, 2027"].expression_id,
    }
    assert expressions["tomorrow"].expression_id not in title_expression_ids

    action = next(
        item
        for item in analysis.mentions
        if item.mention_type == "action" and text[item.start : item.end] == "confirm"
    )
    assert action.segment_id == expressions["tomorrow"].segment_id
    assert action.segment_id != title.segment_id
    assert any(
        lead.mention_id == action.mention_id
        and lead.expression_id == expressions["tomorrow"].expression_id
        for lead in analysis.leads
    )

    plan = plan_gmail_temporal_selector_batches(text=text, analysis=analysis)
    title_candidates = tuple(
        candidate
        for batch in plan.batches
        for candidate in build_gmail_temporal_candidate_frontier(
            analysis=analysis,
            batch=batch,
        ).candidates
        if candidate.subject_mention_id == title.mention_id
    )
    assert {item.normalized_value for item in title_candidates} == {
        "2027-09-25",
        "2027-09-26",
    }


def test_clause_bound_title_does_not_escape_to_later_same_sentence_deadline() -> None:
    text = (
        "Subject: Finance deadline\n\n"
        "The Atlas design review may happen on May 14, 2027 or May 15, 2027, "
        "and I will confirm by May 10, 2027."
    )
    analysis, titles = source_bound_titles(text)
    assert len(titles) == 1
    title = titles[0]
    expressions = {text[item.start : item.end]: item for item in analysis.expressions}
    title_expression_ids = {
        lead.expression_id
        for lead in analysis.leads
        if lead.mention_id == title.mention_id
    }

    assert title_expression_ids == {
        expressions["May 14, 2027"].expression_id,
        expressions["May 15, 2027"].expression_id,
    }
    deadline_expression_id = expressions["May 10, 2027"].expression_id
    assert deadline_expression_id not in title_expression_ids

    plan = plan_gmail_temporal_selector_batches(text=text, analysis=analysis)
    deadline_batch = next(
        batch
        for batch in plan.batches
        if batch.expressions[0].expression_id == deadline_expression_id
    )
    assert (
        "subject_bridge_superseded_by_clause_bound_event_title"
        not in deadline_batch.diagnostics
    )
    deadline_frontier = build_gmail_temporal_candidate_frontier(
        analysis=analysis,
        batch=deadline_batch,
    )
    assert all(
        candidate.subject_mention_id != title.mention_id
        for candidate in deadline_frontier.candidates
    )


@pytest.mark.parametrize(
    ("date_phrase", "expected_dates"),
    (
        (
            "September 25, 2027 or on September 26, 2027",
            {"September 25, 2027", "September 26, 2027"},
        ),
        (
            "September 25, 2027 or September 26, 2027 or September 27, 2027",
            {
                "September 25, 2027",
                "September 26, 2027",
                "September 27, 2027",
            },
        ),
    ),
)
def test_clause_bound_title_retains_common_multi_option_forms(
    date_phrase: str,
    expected_dates: set[str],
) -> None:
    text = f"The Marigold project debrief may happen on {date_phrase}."
    analysis, titles = source_bound_titles(text)
    assert len(titles) == 1
    title = titles[0]
    surfaces = {
        expression.expression_id: text[expression.start : expression.end]
        for expression in analysis.expressions
    }

    assert {
        surfaces[lead.expression_id]
        for lead in analysis.leads
        if lead.mention_id == title.mention_id
    } == expected_dates
    assert analysis.graph_truncated is False
    plan = plan_gmail_temporal_selector_batches(text=text, analysis=analysis)
    assert {
        candidate.normalized_value
        for batch in plan.batches
        for candidate in build_gmail_temporal_candidate_frontier(
            analysis=analysis,
            batch=batch,
        ).candidates
        if candidate.subject_mention_id == title.mention_id
    } == {
        expression.normalized_options[0]
        for expression in analysis.expressions
        if text[expression.start : expression.end] in expected_dates
    }


def test_body_prose_cannot_invent_title_and_suppress_correct_subject_bridge() -> None:
    text = (
        "Subject: Cedar design review\n\n"
        "The Cedar team confirmed the design review is scheduled for June 14, 2027."
    )
    analysis, titles = source_bound_titles(text)

    assert titles == ()
    plan = plan_gmail_temporal_selector_batches(text=text, analysis=analysis)
    assert all(
        "subject_bridge_superseded_by_clause_bound_event_title" not in batch.diagnostics
        for batch in plan.batches
    )
    subject_title = next(
        mention
        for mention in analysis.mentions
        if mention.field == "subject"
        and text[mention.start : mention.end] == "Cedar design review"
    )
    candidates = tuple(
        candidate
        for batch in plan.batches
        for candidate in build_gmail_temporal_candidate_frontier(
            analysis=analysis,
            batch=batch,
        ).candidates
    )
    assert any(
        candidate.subject_mention_id == subject_title.mention_id
        and candidate.normalized_value == "2027-06-14"
        for candidate in candidates
    )


def test_unlisted_reporting_clause_cannot_suppress_correct_subject_bridge() -> None:
    text = (
        "Subject: Lumen review\n\n"
        "The Legal team believes our Lumen review is scheduled for October 7, 2027."
    )
    analysis, titles = source_bound_titles(text)

    assert titles == ()
    plan = plan_gmail_temporal_selector_batches(text=text, analysis=analysis)
    assert all(
        "subject_bridge_superseded_by_clause_bound_event_title" not in batch.diagnostics
        for batch in plan.batches
    )
    subject_title = next(
        mention
        for mention in analysis.mentions
        if mention.field == "subject"
        and text[mention.start : mention.end] == "Lumen review"
    )
    assert any(
        candidate.subject_mention_id == subject_title.mention_id
        and candidate.normalized_value == "2027-10-07"
        for batch in plan.batches
        for candidate in build_gmail_temporal_candidate_frontier(
            analysis=analysis,
            batch=batch,
        ).candidates
    )


@pytest.mark.parametrize(
    "text",
    (
        (
            "Subject: Lumen Quay session moved\n\n"
            "The Lumen Quay planning session has been rescheduled to "
            "September 22, 2027 from September 19, 2027."
        ),
        (
            "Subject: Northstar review cancelled\n\n"
            "The Northstar design review scheduled for October 6, 2027 "
            "has been cancelled."
        ),
    ),
)
def test_clause_bound_identity_supersedes_weaker_subject_bridge(text: str) -> None:
    analysis, titles = source_bound_titles(text)
    assert len(titles) == 1

    plan = plan_gmail_temporal_selector_batches(text=text, analysis=analysis)

    assert plan.batches
    assert all(
        "subject_bridge_superseded_by_clause_bound_event_title" in batch.diagnostics
        for batch in plan.batches
    )
    assert all(
        all(mention.field != "subject" for mention in batch.mentions)
        for batch in plan.batches
    )
    assert all(
        all(context.role != "subject_bridge" for context in batch.contexts)
        for batch in plan.batches
    )


@pytest.mark.parametrize(
    "text",
    (
        "It may happen on September 25, 2027.",
        "The Atlas review may happen to be on September 25, 2027.",
        "The Northstar design review. Scheduled for October 6, 2027.",
        "The Northstar design\nreview scheduled for October 6, 2027.",
        "The Acme parcel delivery is scheduled for September 18, 2027.",
        "The Northstar account review is scheduled for October 6, 2027.",
        "The cedar partner summit is scheduled for October 7, 2027.",
        "> The Northstar design review scheduled for October 6, 2027.\n",
        (
            "Begin forwarded message:\n"
            "The Northstar design review scheduled for October 6, 2027."
        ),
    ),
)
def test_source_bound_event_clause_rejects_unbounded_or_routine_lookalikes(
    text: str,
) -> None:
    _analysis, titles = source_bound_titles(text)

    assert titles == ()
