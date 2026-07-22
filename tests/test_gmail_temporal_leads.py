from __future__ import annotations

import json
from dataclasses import FrozenInstanceError, asdict

import pytest

from pkm_brain.gmail_temporal_leads import (
    TemporalExpression,
    TemporalLocalTime,
    analyze_gmail_temporal_leads,
)


ANCHOR = "2026-12-29T10:00:00-08:00"


def analyze(text: str, *, admitted: bool = True, rescue: bool = False):
    return analyze_gmail_temporal_leads(
        text=text,
        message_internal_at=ANCHOR,
        fact_admitted=admitted,
        temporal_review_rescue=rescue,
        chunk_id="synthetic-message",
    )


def expression_texts(text: str):
    analysis = analyze(text, admitted=False)
    return tuple(text[item.start : item.end] for item in analysis.expressions)


def test_strict_direct_grammar_is_reused_without_broadening_it() -> None:
    strict = analyze("Orchid Interview is scheduled for May 14, 2027.")
    numeric = analyze("Orchid Interview is scheduled for 7/24/2027.")

    direct = [
        item for item in strict.leads if item.association_mode == "direct_grammar"
    ]
    assert len(direct) == 1
    assert direct[0].confidence_tier == "strict_direct"
    assert direct[0].relation == "occurrence"
    assert direct[0].kind == "planned"
    assert direct[0].routable is False
    assert any(
        mention.mention_type == "lifecycle"
        and any(lead.mention_id == mention.mention_id for lead in strict.leads)
        for mention in strict.mentions
    )

    assert all(item.association_mode == "field_local" for item in numeric.leads)
    assert any(item.confidence_tier == "review_resolved" for item in numeric.leads)


def test_fact_admission_gates_only_association_leads() -> None:
    text = "Orchid Interview is scheduled for May 14, 2027."
    held = analyze(text, admitted=False)
    admitted = analyze(text, admitted=True)

    assert held.expressions == admitted.expressions
    assert held.mentions == admitted.mentions
    assert held.leads == ()
    assert admitted.leads
    assert held.fact_admitted is False
    assert admitted.fact_admitted is True


def test_temporal_rescue_adds_review_leads_without_changing_evidence() -> None:
    text = "Orchid Interview is scheduled for May 14, 2027."
    held = analyze(text, admitted=False)
    rescued = analyze(text, admitted=False, rescue=True)

    assert held.expressions == rescued.expressions
    assert held.mentions == rescued.mentions
    assert held.association_admission_basis == "none"
    assert rescued.association_admission_basis == "temporal_rescue"
    assert rescued.fact_admitted is False
    assert rescued.leads


def test_snapshot_fingerprint_and_endpoint_ids_bind_exact_analysis_content() -> None:
    first = analyze("Orchid Interview is scheduled for May 14, 2027.")
    repeated = analyze("Orchid Interview is scheduled for May 14, 2027.")
    changed = analyze("Orchid Workshop is scheduled for May 14, 2027.")

    assert first.snapshot_fingerprint == repeated.snapshot_fingerprint
    assert first.scope_bound is True
    assert first.snapshot_fingerprint.startswith("gta_")
    assert len(first.snapshot_fingerprint) == 68
    assert first.expressions[0].expression_id == repeated.expressions[0].expression_id
    assert first.snapshot_fingerprint != changed.snapshot_fingerprint
    assert first.expressions[0].expression_id != changed.expressions[0].expression_id

    anonymous = analyze_gmail_temporal_leads(
        text="Orchid Interview is scheduled for May 14, 2027.",
        message_internal_at=ANCHOR,
        fact_admitted=True,
        chunk_id=None,
    )
    assert anonymous.scope_bound is False


def test_field_local_association_supports_both_orders_and_soft_newlines() -> None:
    event_first = analyze("Orchid Interview\nMay 14, 2027")
    date_first = analyze("May 14, 2027\nOrchid Interview")

    assert [item.association_mode for item in event_first.leads] == ["field_local"]
    assert [item.association_mode for item in date_first.leads] == ["field_local"]
    assert event_first.leads[0].gap_chars == 1
    assert date_first.leads[0].gap_chars <= 60
    assert event_first.leads[0].relation == "occurrence"
    assert date_first.leads[0].kind is None


def test_sentence_punctuation_blocks_local_but_layout_newlines_stay_soft() -> None:
    sentence_break = analyze("Orchid Interview. May 14, 2027")
    blank_line = analyze("Orchid Interview\n\nMay 14, 2027")

    assert [item.association_mode for item in sentence_break.leads] == ["field_near"]
    assert [item.association_mode for item in blank_line.leads] == ["field_local"]
    assert all(
        item.confidence_tier == "review_ambiguous" for item in sentence_break.leads
    )
    assert "field_near_review_only" in sentence_break.leads[0].blockers
    assert "sentence_punctuation_crossing" in sentence_break.leads[0].risk_features
    assert blank_line.leads[0].confidence_tier == "review_resolved"


def test_subject_and_message_singleton_fallbacks_are_explicit() -> None:
    subject = analyze(
        "Subject: Orchid Interview\n\nPlease keep July 22, 2027 available."
    )
    message = analyze(
        "Orchid Interview " + ("background context " * 20) + "July 22, 2027"
    )

    assert [item.association_mode for item in subject.leads] == [
        "subject_body_bridge",
        "subject_body_bridge",
    ]
    subject_mentions = {item.mention_id: item.mention_type for item in subject.mentions}
    assert {
        subject_mentions[item.mention_id] for item in subject.leads
    } == {"event", "event_title_candidate"}
    assert all(item.confidence_tier == "review_ambiguous" for item in subject.leads)
    assert [item.association_mode for item in message.leads] == ["message_singleton"]
    assert message.leads[0].gap_chars > 60


def test_subject_body_bridge_keeps_multiple_date_alternatives_review_only() -> None:
    result = analyze(
        "Subject: Project Interview\n\n"
        "Possible dates are July 22, 2027 or July 24, 2027."
    )

    assert len(result.expressions) == 2
    assert [item.association_mode for item in result.leads] == [
        "subject_body_bridge",
        "subject_body_bridge",
        "subject_body_bridge",
        "subject_body_bridge",
    ]
    mention_types = {item.mention_id: item.mention_type for item in result.mentions}
    assert {
        mention_types[item.mention_id] for item in result.leads
    } == {"event", "event_title_candidate"}
    assert all(
        "subject_body_bridge_review_only" in item.blockers for item in result.leads
    )
    assert all(
        "cross_field_subject_body" in item.risk_features for item in result.leads
    )
    assert all(item.confidence_tier == "review_ambiguous" for item in result.leads)
    assert all(item.routable is False for item in result.leads)


def test_subject_body_bridge_graph_is_degree_bounded_and_reports_truncation() -> None:
    dates = " ".join(f"July {day}, 2027" for day in range(1, 11))
    result = analyze(f"Subject: Meeting Call Workshop Interview\n\n{dates}")

    assert len(result.expressions) == 10
    assert result.candidate_edge_count > result.retained_edge_count
    assert len(result.leads) <= 12
    assert all(item.association_mode == "subject_body_bridge" for item in result.leads)
    assert result.graph_truncated is True


def test_near_field_edges_are_bounded_review_hints_with_visible_risks() -> None:
    long_gap = analyze("Project launch " + ("x" * 80) + " July 22, 2027")
    sentence = analyze(
        "Project launch. Details follow and remain relevant. July 22, 2027"
    )

    assert [item.association_mode for item in long_gap.leads] == ["field_near"]
    assert "field_near_review_only" in long_gap.leads[0].blockers
    assert "long_association_gap" in long_gap.leads[0].risk_features
    assert long_gap.leads[0].gap_chars <= 240

    assert [item.association_mode for item in sentence.leads] == ["field_near"]
    assert "sentence_punctuation_crossing" in sentence.leads[0].risk_features
    assert "artifact_context" in sentence.leads[0].blockers


def test_lifecycle_mentions_are_edgeable_without_inventing_start_semantics() -> None:
    for role, text in (
        ("cancelled", "Cancelled on July 22, 2027"),
        ("completed", "Completed July 22, 2027"),
    ):
        result = analyze(text)
        mention = next(
            item
            for item in result.mentions
            if item.mention_type == "lifecycle" and item.lifecycle_role == role
        )
        lead = next(
            item for item in result.leads if item.mention_id == mention.mention_id
        )

        assert mention.relation is None
        assert mention.kind is None
        assert lead.relation is None
        assert lead.kind is None
        assert f"lifecycle_{role}" in lead.blockers
        assert lead.confidence_tier == "review_ambiguous"
        assert lead.routable is False


@pytest.mark.parametrize(
    "text",
    (
        "The meeting took place on May 14, 2027.",
        "The meeting was held on May 14, 2027.",
    ),
)
def test_actual_occurrence_cues_are_not_completion_lifecycle(text: str) -> None:
    result = analyze(text)

    assert not any(
        item.lifecycle_role == "completed" for item in result.mentions
    )
    assert any(
        lead.relation == "occurrence"
        and lead.kind == "actual"
        and "lifecycle_completed" not in lead.blockers
        for lead in result.leads
    )


def test_structural_labels_and_common_predicates_expand_the_mention_inventory() -> None:
    labelled = analyze("When:\nJuly 22, 2027")
    event_predicate = analyze("We will meet on July 22, 2027")
    action_predicate = analyze("Please confirm your attendance by July 22, 2027")

    label = next(
        item for item in labelled.mentions if item.mention_type == "structural_label"
    )
    label_lead = next(
        item for item in labelled.leads if item.mention_id == label.mention_id
    )
    assert label_lead.association_mode == "field_local"
    assert (label_lead.relation, label_lead.kind) == ("occurrence", "planned")

    predicate = next(
        item
        for item in event_predicate.mentions
        if item.mention_type == "event_predicate"
    )
    predicate_lead = next(
        item
        for item in event_predicate.leads
        if item.mention_id == predicate.mention_id
    )
    assert (predicate_lead.relation, predicate_lead.kind) == (
        "occurrence",
        "planned",
    )
    assert "predicate_mention_review_only" in predicate_lead.blockers

    action = next(
        item for item in action_predicate.mentions if item.mention_type == "action"
    )
    action_lead = next(
        item for item in action_predicate.leads if item.mention_id == action.mention_id
    )
    assert (action_lead.relation, action_lead.kind) == ("deadline", "planned")


def test_structured_when_label_creates_deferred_subject_event_title_candidate() -> None:
    text = "Subject: Q3 Leadership Forum\n\nWhen: May 14, 2027"
    result = analyze(text)

    title = next(
        item
        for item in result.mentions
        if item.mention_type == "event_title_candidate"
    )
    assert text[title.start : title.end] == "Q3 Leadership Forum"
    assert title.field == "subject"
    assert title.segment_id.startswith("subject:")
    assert title.blockers == ("event_title_review_only",)
    assert any(
        lead.mention_id == title.mention_id
        and lead.association_mode == "subject_body_bridge"
        for lead in result.leads
    )


def test_specific_event_title_coexists_with_its_generic_event_noun() -> None:
    text = "Subject: Orchid Interview\n\nWhen: May 14, 2027"
    result = analyze(text)

    title = next(
        item
        for item in result.mentions
        if item.mention_type == "event_title_candidate"
    )
    generic = next(
        item
        for item in result.mentions
        if item.mention_type == "event" and text[item.start : item.end] == "Interview"
    )

    assert text[title.start : title.end] == "Orchid Interview"
    assert title.start < generic.start < generic.end <= title.end


def test_specific_subject_event_title_can_use_an_unlabeled_body_time() -> None:
    text = "Subject: Orchid Interview\n\nPlease join us on May 14, 2027."
    result = analyze(text)

    title = next(
        item
        for item in result.mentions
        if item.mention_type == "event_title_candidate"
    )

    assert text[title.start : title.end] == "Orchid Interview"
    assert "event_title_review_only" in title.blockers


def test_date_only_or_unstructured_subject_does_not_become_event_title() -> None:
    date_only = analyze("Subject: May 14, 2027\n\nWhen: May 15, 2027")
    no_title = analyze("When: May 14, 2027")

    assert not any(
        item.mention_type == "event_title_candidate"
        for item in (*date_only.mentions, *no_title.mentions)
    )


@pytest.mark.parametrize(
    "subject",
    (
        "Save 20% on summer travel",
        "Your order update",
    ),
)
def test_structured_time_label_does_not_turn_non_event_subject_into_event_title(
    subject: str,
) -> None:
    result = analyze(f"Subject: {subject}\n\nWhen: May 14, 2027")

    assert not any(
        item.mention_type == "event_title_candidate"
        for item in result.mentions
    )


def test_sentence_segments_are_preserved_on_expression_and_mention_endpoints() -> None:
    text = (
        "Alpha meeting was cancelled May 14, 2027. "
        "Beta workshop is May 15, 2027."
    )
    result = analyze(text)

    assert len({item.segment_id for item in result.expressions}) == 2
    event_segments = {
        text[item.start : item.end].casefold(): item.segment_id
        for item in result.mentions
        if item.mention_type == "event"
    }
    assert event_segments["meeting"] != event_segments["workshop"]


@pytest.mark.parametrize(
    ("text", "expected_form", "expected_options", "expected_status"),
    (
        (
            "Meeting 7/8/2027",
            "numeric_date",
            ("2027-07-08", "2027-08-07"),
            "ambiguous",
        ),
        (
            "Meeting 7/24/2027",
            "numeric_date",
            ("2027-07-24",),
            "resolved",
        ),
        (
            "Meeting 2027/7/24",
            "numeric_date",
            ("2027-07-24",),
            "resolved",
        ),
        (
            "Meeting yesterday",
            "relative_date",
            ("2026-12-28",),
            "resolved",
        ),
        (
            "Meeting tonight",
            "relative_date",
            ("2026-12-29",),
            "resolved",
        ),
        (
            "Meeting in 3 days",
            "relative_date",
            ("2027-01-01",),
            "resolved",
        ),
        ("Meeting at 3:30 pm", "time_only", (), "unresolved"),
    ),
)
def test_high_recall_expression_inventory(
    text: str,
    expected_form: str,
    expected_options: tuple[str, ...],
    expected_status: str,
) -> None:
    result = analyze(text, admitted=False)
    assert len(result.expressions) == 1
    expression = result.expressions[0]
    assert expression.form == expected_form
    assert expression.normalized_options == expected_options
    assert expression.resolution_status == expected_status
    assert text[expression.start : expression.end] in expression_texts(text)


@pytest.mark.parametrize(
    ("text", "surface", "form", "blocker"),
    (
        (
            "The workshop is next week.",
            "next week",
            "coarse_relative",
            "coarse_relative_unresolved",
        ),
        (
            "Please reply within three days.",
            "within three days",
            "coarse_relative",
            "coarse_relative_unresolved",
        ),
        (
            "The sync happens every Tuesday.",
            "every Tuesday",
            "recurrence",
            "recurrence_not_expanded",
        ),
        (
            "A monthly review is planned.",
            "monthly",
            "recurrence",
            "recurrence_not_expanded",
        ),
    ),
)
def test_coarse_and_recurring_expressions_are_inventoried_but_not_normalized(
    text: str,
    surface: str,
    form: str,
    blocker: str,
) -> None:
    result = analyze(text, admitted=False)
    expression = next(item for item in result.expressions if item.form == form)

    assert text[expression.start : expression.end] == surface
    assert expression.normalized_options == ()
    assert expression.resolution_status == "unresolved"
    assert blocker in expression.blockers


@pytest.mark.parametrize(
    ("text", "expected_day"),
    (
        ("The interview is tomorrow morning.", "2026-12-30"),
        ("The interview is this morning.", "2026-12-29"),
        ("The interview is today evening.", "2026-12-29"),
    ),
)
def test_coarse_time_of_day_preserves_its_anchored_calendar_day(
    text: str,
    expected_day: str,
) -> None:
    expression = analyze(text, admitted=False).expressions[0]

    assert expression.form == "coarse_relative"
    assert expression.normalized_options == (expected_day,)
    assert expression.calendar_date_options == (expected_day,)
    assert expression.resolution_status == "resolved"
    assert "relative_to_message_time" in expression.blockers
    assert "time_of_day_unresolved" in expression.blockers


def test_weekday_conventions_are_preserved_as_options_not_guessed() -> None:
    bare = analyze("Meeting Friday", admitted=False).expressions[0]
    qualified = analyze("Meeting this coming Friday", admitted=False).expressions[0]
    next_weekday = analyze("Meeting next Friday", admitted=False).expressions[0]

    assert bare.normalized_options == ("2026-12-25", "2027-01-01")
    assert "ambiguous_weekday_convention" in bare.blockers
    assert qualified.normalized_options == ("2027-01-01",)
    assert next_weekday.normalized_options == ("2027-01-01", "2027-01-08")
    assert next_weekday.resolution_status == "ambiguous"


def test_explicit_offset_normalizes_exact_time_while_abbreviation_stays_blocked() -> (
    None
):
    numeric = analyze("Orchid Interview is scheduled for May 14, 2027 at 16:30 -07:00.")
    abbreviation = analyze(
        "Orchid Interview is scheduled for May 14, 2027 at 4:30 PM PDT."
    )

    assert numeric.expressions[0].normalized_options == ("2027-05-14T16:30:00-07:00",)
    assert numeric.expressions[0].precision == "exact"
    assert "explicit_numeric_utc_offset" in numeric.expressions[0].resolution_basis
    assert (
        "timezone_abbreviation_requires_review" in abbreviation.expressions[0].blockers
    )
    assert abbreviation.leads[0].confidence_tier == "review_ambiguous"


def test_action_deadlines_are_recognized_in_either_order() -> None:
    action_first = analyze("Submit by July 24, 2027")
    date_first = analyze("By July 24, 2027, submit")

    assert action_first.leads[0].relation == "deadline"
    assert action_first.leads[0].kind == "planned"
    assert date_first.leads[0].relation == "deadline"
    assert date_first.leads[0].kind == "planned"


def test_lifecycle_artifact_boundary_and_multiple_options_are_blockers() -> None:
    artifact = analyze("The meeting notes were completed on July 22, 2027.")
    lifecycle = analyze(
        "The meeting was rescheduled from July 20, 2027 to July 22, 2027."
    )
    boundary = analyze("The flight arrives July 22, 2027")
    ambiguous = analyze("The meeting is scheduled for 7/8/2027")

    assert {"artifact_context", "lifecycle_completed"}.issubset(
        set(artifact.leads[0].blockers)
    )
    assert "lifecycle_rescheduled" in lifecycle.leads[0].blockers
    assert "terminal_boundary_not_occurrence_start" in boundary.leads[0].blockers
    assert "multiple_normalization_options" in ambiguous.leads[0].blockers
    assert all(
        item.confidence_tier == "review_ambiguous"
        for item in (
            artifact.leads[0],
            lifecycle.leads[0],
            boundary.leads[0],
            ambiguous.leads[0],
        )
    )


def test_marker_backed_footer_is_blocked_but_generic_tail_is_not_invented() -> None:
    footer = analyze("Meeting July 22, 2027\nunsubscribe")
    generic = analyze("Meeting July 22, 2027\ngeneral closing text")

    assert "footer_marker_context" in footer.leads[0].blockers
    assert not any(mention.mention_type == "artifact" for mention in generic.mentions)


def test_quoted_line_temporal_evidence_is_inventoried_and_blocked() -> None:
    text = (
        "Subject: Status update\n\n"
        "> Meeting is scheduled for May 14, 2027."
    )
    result = analyze(text)

    assert result.expressions
    assert result.mentions
    assert result.leads
    assert all(
        "quoted_or_forwarded_context" in item.blockers
        for item in (*result.expressions, *result.mentions, *result.leads)
    )
    assert all(item.confidence_tier == "review_ambiguous" for item in result.leads)


@pytest.mark.parametrize(
    "marker",
    (
        "-----Original Message-----",
        "---------- Forwarded message ---------",
        "Begin forwarded message:",
        "On Tuesday, May 4, 2027 Pat wrote:",
    ),
)
def test_forwarded_or_original_tail_temporal_evidence_is_blocked(
    marker: str,
) -> None:
    text = (
        f"Subject: Status update\n\n{marker}\n"
        "Meeting is scheduled for May 14, 2027."
    )
    result = analyze(text)

    assert result.expressions
    assert result.mentions
    assert result.leads
    assert all(
        "quoted_or_forwarded_context" in item.blockers
        for item in (*result.expressions, *result.mentions, *result.leads)
    )


def test_authored_temporal_evidence_before_quote_remains_unblocked() -> None:
    text = (
        "Subject: Status update\n\n"
        "Authored meeting is scheduled for May 13, 2027.\n\n"
        "> Quoted meeting is scheduled for May 14, 2027."
    )
    result = analyze(text)
    authored_expression = next(
        item
        for item in result.expressions
        if text[item.start : item.end] == "May 13, 2027"
    )
    quoted_expression = next(
        item
        for item in result.expressions
        if text[item.start : item.end] == "May 14, 2027"
    )
    authored_mention = next(
        item
        for item in result.mentions
        if item.mention_type == "event"
        and text[item.start : item.end].casefold() == "meeting"
        and item.start < text.index(">")
    )

    assert "quoted_or_forwarded_context" not in authored_expression.blockers
    assert "quoted_or_forwarded_context" not in authored_mention.blockers
    assert "quoted_or_forwarded_context" in quoted_expression.blockers
    assert any(
        lead.expression_id == authored_expression.expression_id
        and lead.mention_id == authored_mention.mention_id
        and "quoted_or_forwarded_context" not in lead.blockers
        for lead in result.leads
    )


def test_nearest_edge_selection_caps_dense_cartesian_pairing() -> None:
    text = (
        "Meeting call session July 20, 2027 July 21, 2027 July 22, 2027 "
        "workshop interview review"
    )
    result = analyze(text)

    assert len(result.expressions) == 3
    assert len([item for item in result.mentions if item.mention_type == "event"]) == 6
    assert 0 < len(result.leads) <= 12
    assert result.candidate_edge_count >= result.retained_edge_count
    assert result.graph_truncated is True


def test_no_event_or_action_mention_means_no_association() -> None:
    result = analyze("Please keep July 22, 2027 in mind.")

    assert len(result.expressions) == 1
    assert result.mentions == ()
    assert result.leads == ()


def test_results_are_content_free_deterministic_and_frozen() -> None:
    private_marker = "SYNTHETIC_PRIVATE_MARKER"
    text = (
        f"{private_marker} Orchid Interview is scheduled for May 14, 2027 "
        "at 16:30 -07:00."
    )
    first = analyze(text)
    second = analyze(text)

    assert first == second
    serialized = json.dumps(asdict(first), sort_keys=True)
    assert private_marker not in serialized
    assert "Orchid" not in serialized
    assert "Interview" not in serialized
    assert text[first.expressions[0].start : first.expressions[0].end] == (
        "May 14, 2027 at 16:30 -07:00"
    )
    with pytest.raises(FrozenInstanceError):
        first.expressions[0].start = 0  # type: ignore[misc]


def test_analysis_requires_an_explicit_admission_argument() -> None:
    with pytest.raises(TypeError):
        analyze_gmail_temporal_leads(  # type: ignore[call-arg]
            text="Meeting July 22, 2027",
            message_internal_at=ANCHOR,
        )


def test_public_expression_dataclass_has_no_source_content_field() -> None:
    assert set(TemporalExpression.__dataclass_fields__) == {
        "expression_id",
        "start",
        "end",
        "field",
        "segment_id",
        "form",
        "normalized_options",
        "calendar_date_options",
        "local_time",
        "precision",
        "resolution_status",
        "resolution_basis",
        "blockers",
    }


def test_invalid_dates_and_ranges_are_explicitly_unresolved_and_blocked() -> None:
    invalid_date = analyze("Meeting February 30, 2027")
    invalid_range = analyze("Workshop May 20-14, 2027")

    assert invalid_date.expressions[0].normalized_options == ()
    assert invalid_date.expressions[0].resolution_status == "unresolved"
    assert "invalid_calendar_date" in invalid_date.expressions[0].blockers
    assert "normalization_not_single_complete_value" in invalid_date.leads[0].blockers
    assert invalid_date.leads[0].confidence_tier == "review_ambiguous"

    assert invalid_range.expressions[0].normalized_options == ()
    assert "invalid_or_nonascending_range" in invalid_range.expressions[0].blockers
    assert invalid_range.leads[0].confidence_tier == "review_ambiguous"


def test_unzoned_wall_times_are_typed_but_never_complete_normalizations() -> None:
    date_time = analyze("Meeting May 14, 2027 at 9:00 AM")
    time_only = analyze("Meeting at 3:30 PM")

    combined = date_time.expressions[0]
    assert combined.normalized_options == ()
    assert combined.calendar_date_options == ("2027-05-14",)
    assert combined.resolution_status == "unresolved"
    assert combined.local_time == TemporalLocalTime(
        hour_options=(9,),
        minute=0,
        second=0,
        microsecond=0,
        timezone_basis=None,
        utc_offset_minutes=None,
        zone_identifier=None,
    )
    assert "missing_timezone" in combined.blockers

    clock = time_only.expressions[0]
    assert clock.normalized_options == ()
    assert clock.calendar_date_options == ()
    assert clock.local_time and clock.local_time.hour_options == (15,)
    assert "missing_calendar_date" in clock.blockers


def test_month_day_clock_and_relative_clock_are_composed_without_guessing() -> None:
    missing_year = analyze("Meeting May 14, 9:00 AM")
    relative_zoned = analyze("Meeting tomorrow at 3:30 PM -08:00")
    relative_unzoned = analyze("Meeting tomorrow at 3:30 PM")

    expression = missing_year.expressions[0]
    assert expression_texts("Meeting May 14, 9:00 AM") == ("May 14, 9:00 AM",)
    assert expression.normalized_options == ()
    assert expression.calendar_date_options == ("2027-05-14", "2026-05-14")
    assert expression.local_time and expression.local_time.hour_options == (9,)

    zoned = relative_zoned.expressions[0]
    assert zoned.normalized_options == ("2026-12-30T15:30:00-08:00",)
    assert zoned.calendar_date_options == ("2026-12-30",)
    assert zoned.local_time and zoned.local_time.utc_offset_minutes == -480

    unzoned = relative_unzoned.expressions[0]
    assert unzoned.normalized_options == ()
    assert unzoned.calendar_date_options == ("2026-12-30",)
    assert unzoned.local_time and unzoned.local_time.hour_options == (15,)


def test_reschedule_from_to_retains_endpoints_instead_of_inventing_interval() -> None:
    result = analyze("The meeting was rescheduled from July 20, 2027 to July 22, 2027.")

    assert [item.form for item in result.expressions] == [
        "explicit_date",
        "explicit_date",
    ]
    assert [item.normalized_options for item in result.expressions] == [
        ("2027-07-20",),
        ("2027-07-22",),
    ]
    assert all("lifecycle_rescheduled" in item.blockers for item in result.leads)
    assert all(item.confidence_tier == "review_ambiguous" for item in result.leads)


def test_missing_year_keeps_adjacent_year_suggestions_ambiguous() -> None:
    expression = analyze("Meeting May 14", admitted=False).expressions[0]

    assert expression.normalized_options == ("2027-05-14", "2026-05-14")
    assert expression.resolution_status == "ambiguous"
    assert "inferred_year_from_message_time" in expression.blockers
    assert "multiple_normalization_options" in expression.blockers


def test_strict_direct_ignores_unrelated_artifact_and_kind_is_clause_bound() -> None:
    strict = analyze(
        "Orchid Interview is scheduled for May 14, 2027. See attachment details."
    )
    separate_clause = analyze("Meeting happened. Call July 22, 2027")

    assert strict.leads[0].association_mode == "direct_grammar"
    assert strict.leads[0].confidence_tier == "strict_direct"
    assert "artifact_context" not in strict.leads[0].blockers
    call_lead = next(
        item
        for item in separate_clause.leads
        if separate_clause.mentions[
            next(
                index
                for index, mention in enumerate(separate_clause.mentions)
                if mention.mention_id == item.mention_id
            )
        ].start
        > separate_clause.mentions[0].start
    )
    assert call_lead.kind is None


def test_ids_are_chunk_scoped_opaque_and_graph_accounting_is_explicit() -> None:
    private_chunk = "PRIVATE_PROVIDER_MESSAGE_IDENTIFIER"
    first = analyze_gmail_temporal_leads(
        text="Meeting July 22, 2027",
        message_internal_at=ANCHOR,
        fact_admitted=True,
        chunk_id=private_chunk,
    )
    second = analyze_gmail_temporal_leads(
        text="Meeting July 22, 2027",
        message_internal_at=ANCHOR,
        fact_admitted=True,
        chunk_id="different-private-message",
    )

    first_ids = {
        *(item.expression_id for item in first.expressions),
        *(item.mention_id for item in first.mentions),
        *(item.lead_id for item in first.leads),
    }
    second_ids = {
        *(item.expression_id for item in second.expressions),
        *(item.mention_id for item in second.mentions),
        *(item.lead_id for item in second.leads),
    }
    assert first_ids.isdisjoint(second_ids)
    assert all(private_chunk not in value for value in first_ids)
    assert private_chunk not in json.dumps(asdict(first), sort_keys=True)
    assert first.candidate_edge_count == first.retained_edge_count == 1
    assert first.candidate_edge_count_exact is True
    assert first.omitted_expression_count == first.omitted_mention_count == 0
    assert first.graph_truncated is False


def test_large_inventories_are_lossless_but_hint_construction_is_bounded() -> None:
    text = (
        "Subject: "
        + " ".join("meeting" for _ in range(160))
        + "\n\n"
        + " ".join("May 14, 2027" for _ in range(400))
    )

    result = analyze(text)

    assert len(result.expressions) == 400
    assert len([item for item in result.mentions if item.mention_type == "event"]) >= 160
    assert result.graph_truncated is True
    assert result.candidate_edge_count_exact is False
    assert result.omitted_expression_count > 0
    assert result.omitted_mention_count > 0
    assert len(result.leads) <= 52
    assert all(
        "association_inventory_truncated" in item.blockers
        for item in result.leads
        if item.association_mode != "direct_grammar"
    )


def test_layout_break_is_a_risk_not_a_blocker_and_clock_noise_is_narrowed() -> None:
    layout = analyze("Meeting\n\nJuly 22, 2027")
    clocks = analyze("version v3:30 and ref 12:45; Meeting at 16:30", admitted=False)

    assert layout.leads[0].association_mode == "field_local"
    assert layout.leads[0].confidence_tier == "review_resolved"
    assert "layout_break_crossing" in layout.leads[0].risk_features
    assert "layout_break_crossing" not in layout.leads[0].blockers
    assert len(clocks.expressions) == 1
    assert clocks.expressions[0].local_time
    assert clocks.expressions[0].local_time.hour_options == (16,)
