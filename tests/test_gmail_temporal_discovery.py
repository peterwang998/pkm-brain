from __future__ import annotations

from dataclasses import asdict

from pkm_brain.gmail_temporal_discovery import (
    GmailTemporalCandidate,
    discover_gmail_temporal_candidates,
)


ANCHOR = "2026-12-29T10:00:00-08:00"


def discover(text: str, *, anchor: str = ANCHOR) -> tuple[GmailTemporalCandidate, ...]:
    return discover_gmail_temporal_candidates(
        text=text,
        message_internal_at=anchor,
        chunk_id="synthetic-chunk",
    )


def test_explicit_full_date_returns_exact_half_open_source_spans() -> None:
    text = "Orchid Interview is scheduled for May 14, 2027."
    (candidate,) = discover(text)

    assert candidate.relation == "occurrence"
    assert candidate.kind == "planned"
    assert candidate.start_at == "2027-05-14"
    assert candidate.end_at is None
    assert candidate.precision == "day"
    assert candidate.resolution_basis == ("explicit_month_day_year",)
    assert text[candidate.expression_span.start : candidate.expression_span.end] == (
        "May 14, 2027"
    )
    assert text[candidate.cue_span.start : candidate.cue_span.end] == (
        "is scheduled for"
    )
    assert candidate.expression_span.chunk_id == "synthetic-chunk"


def test_month_day_infers_nearest_bounded_year_from_provider_time() -> None:
    text = "Orchid Interview is scheduled for January 3."
    (candidate,) = discover(text)

    assert candidate.start_at == "2027-01-03"
    assert candidate.resolution_basis == (
        "month_day_year_inferred_from_message_internal_at",
    )


def test_relative_dates_are_anchored_but_bare_weekday_is_not_guessed() -> None:
    today = discover("Orchid Interview is scheduled for today.")
    tomorrow = discover("Orchid Interview is scheduled for tomorrow.")
    coming = discover("Orchid Interview is scheduled for this coming Friday.")
    bare = discover("Orchid Interview is scheduled for Friday.")

    assert today[0].start_at == "2026-12-29"
    assert tomorrow[0].start_at == "2026-12-30"
    assert coming[0].start_at == "2027-01-01"
    assert bare == ()


def test_textual_range_has_inclusive_evidence_and_exclusive_end_bound() -> None:
    text = "Cedar Workshop is scheduled for May 14-16, 2027."
    (candidate,) = discover(text)

    assert candidate.start_at == "2027-05-14"
    assert candidate.end_at == "2027-05-17"
    assert candidate.precision == "day"
    assert candidate.resolution_basis == (
        "textual_range_with_explicit_year",
        "inclusive_textual_date_range",
    )
    assert text[candidate.expression_span.start : candidate.expression_span.end] == (
        "May 14-16, 2027"
    )


def test_until_range_uses_an_exclusive_end_date() -> None:
    text = "Cedar Workshop is scheduled for May 14, 2027 until May 16, 2027."
    (candidate,) = discover(text)

    assert candidate.start_at == "2027-05-14"
    assert candidate.end_at == "2027-05-16"
    assert "exclusive_until_date_range" in candidate.resolution_basis
    assert text[candidate.expression_span.start : candidate.expression_span.end] == (
        "May 14, 2027 until May 16, 2027"
    )


def test_repeated_month_and_cross_year_ranges_are_not_suppressed() -> None:
    repeated_month = discover("Cedar Workshop is scheduled for May 14 to May 16.")
    cross_year = discover(
        "Cedar Workshop is scheduled for December 30 to January 2, 2027."
    )

    assert repeated_month[0].start_at == "2027-05-14"
    assert repeated_month[0].end_at == "2027-05-17"
    assert cross_year[0].start_at == "2026-12-30"
    assert cross_year[0].end_at == "2027-01-03"


def test_common_clock_with_explicit_offset_is_normalized_exactly() -> None:
    text = "Orchid Interview is scheduled for May 14, 2027 at 4:30 PM PDT."
    (candidate,) = discover(text)

    assert candidate.start_at == "2027-05-14T16:30:00-07:00"
    assert candidate.precision == "exact"
    assert candidate.resolution_basis == (
        "explicit_month_day_year",
        "explicit_clock_time",
        "fixed_north_american_timezone_abbreviation",
    )
    assert text[candidate.expression_span.start : candidate.expression_span.end] == (
        "May 14, 2027 at 4:30 PM PDT"
    )


def test_iso_clock_with_seconds_and_numeric_offset_is_preserved() -> None:
    text = "Orchid Interview is scheduled for 2027-05-14T16:30:45.125-07:00."
    (candidate,) = discover(text)

    assert candidate.start_at == "2027-05-14T16:30:45.125000-07:00"
    assert text[candidate.expression_span.start : candidate.expression_span.end] == (
        "2027-05-14T16:30:45.125-07:00"
    )


def test_iana_timezone_uses_date_specific_offset() -> None:
    text = (
        "Orchid Interview is scheduled for May 14, 2027 at 16:30 America/Los_Angeles."
    )
    (candidate,) = discover(text)

    assert candidate.start_at == "2027-05-14T16:30:00-07:00"
    assert "explicit_iana_timezone" in candidate.resolution_basis


def test_iana_timezone_rejects_nonexistent_and_ambiguous_local_times() -> None:
    nonexistent = discover(
        "Orchid Interview is scheduled for March 14, 2027 at 2:30 AM "
        "America/Los_Angeles."
    )
    ambiguous = discover(
        "Orchid Interview is scheduled for November 7, 2027 at 1:30 AM "
        "America/Los_Angeles."
    )

    assert nonexistent[0].start_at == "2027-03-14"
    assert ambiguous[0].start_at == "2027-11-07"
    assert nonexistent[0].resolution_basis[-1] == (
        "clock_time_discarded_invalid_or_ambiguous_timezone"
    )
    assert ambiguous[0].resolution_basis[-1] == (
        "clock_time_discarded_invalid_or_ambiguous_timezone"
    )


def test_clock_without_meridiem_requires_an_unambiguous_24_hour_form() -> None:
    ambiguous = discover("Orchid Interview is scheduled for May 14, 2027 at 4:30 PDT.")
    explicit_24_hour = discover(
        "Orchid Interview is scheduled for May 14, 2027 at 16:30 PDT."
    )

    assert ambiguous[0].start_at == "2027-05-14"
    assert ambiguous[0].resolution_basis[-1] == (
        "clock_time_discarded_ambiguous_meridiem"
    )
    assert explicit_24_hour[0].start_at == "2027-05-14T16:30:00-07:00"


def test_clock_without_timezone_is_discarded_but_supported_day_survives() -> None:
    text = "Orchid Interview is scheduled for May 14, 2027 at 4:30 PM."
    (candidate,) = discover(text)

    assert candidate.start_at == "2027-05-14"
    assert candidate.precision == "day"
    assert candidate.resolution_basis[-1] == ("clock_time_discarded_missing_timezone")
    assert text[candidate.expression_span.start : candidate.expression_span.end] == (
        "May 14, 2027"
    )


def test_deadline_cues_are_end_only_and_generic_by_is_rejected() -> None:
    due = discover("The application is due by January 3, 2027.")
    action = discover("Please submit the application by January 3, 2027.")
    generic = discover("The package was carried by January 3, 2027.")

    assert due[0].relation == "deadline"
    assert due[0].start_at is None
    assert due[0].end_at == "2027-01-03"
    assert action[0].relation == "deadline"
    assert generic == ()


def test_audit_dates_do_not_masquerade_as_deadlines() -> None:
    updated = discover("The application deadline was updated on May 14, 2027.")
    announced = discover("The application deadline was announced on May 14, 2027.")
    extended = discover("The application deadline was extended to May 14, 2027.")

    assert updated == ()
    assert announced == ()
    assert extended[0].end_at == "2027-05-14"


def test_cross_predicate_by_date_is_not_an_action_deadline() -> None:
    text = "Submit the application, and it was reviewed by May 14, 2027."

    assert discover(text) == ()


def test_actual_occurrence_is_distinct_from_planned_occurrence() -> None:
    (candidate,) = discover("Orchid Interview occurred on January 3, 2027.")

    assert candidate.relation == "occurrence"
    assert candidate.kind == "actual"


def test_parent_event_end_boundaries_are_never_emitted_as_starts() -> None:
    unsafe_boundaries = (
        "Northwind flight NW 331 arrives May 14, 2027.",
        "Northwind flight NW 331 will arrive on May 14, 2027.",
        "Cedar Workshop ends on May 14, 2027.",
        "Cedar Workshop arrived on May 14, 2027.",
        "Cedar Workshop ended on May 14, 2027.",
        "Cedar Workshop was completed on May 14, 2027.",
    )

    for text in unsafe_boundaries:
        assert discover(text) == ()


def test_direct_occurrence_and_start_cues_remain_supported() -> None:
    samples = (
        "Northwind flight NW 331 is scheduled for May 14, 2027.",
        "Northwind flight NW 331 departs on May 14, 2027.",
        "Cedar Workshop starts on May 14, 2027.",
        "Cedar Workshop occurred on May 14, 2027.",
    )

    for text in samples:
        (candidate,) = discover(text)
        assert candidate.start_at == "2027-05-14"


def test_named_boundary_requires_a_separate_unambiguous_event_noun() -> None:
    assert discover("Northwind Flight Arrival is scheduled for May 14, 2027.") == ()
    assert discover("Cedar Workshop Completion is scheduled for May 14, 2027.") == ()

    (ceremony,) = discover("Cedar Completion Ceremony is scheduled for May 14, 2027.")
    assert ceremony.start_at == "2027-05-14"


def test_date_weekday_must_agree_when_both_are_present() -> None:
    correct = discover("Orchid Interview is scheduled for Friday, May 14, 2027.")
    contradictory = discover("Orchid Interview is scheduled for Monday, May 14, 2027.")

    assert correct[0].start_at == "2027-05-14"
    assert contradictory == ()


def test_non_event_and_confirmation_action_do_not_produce_occurrence() -> None:
    assert discover("Alice will be in Paris on January 3, 2027.") == ()
    assert (
        discover(
            "The Cedar reservation was confirmed in our system on January 3, 2027."
        )
        == ()
    )


def test_event_artifacts_and_relative_clauses_do_not_create_occurrences() -> None:
    assert discover("Meeting notes were completed on May 14, 2027.") == ()
    assert discover("The meeting reminder is scheduled for May 14, 2027.") == ()
    assert (
        discover(
            "Orchid Interview is scheduled for Alice, whose birthday is May 14, 2027."
        )
        == ()
    )
    assert discover("The interview started planning on May 14, 2027.") == ()


def test_malformed_short_year_is_not_partially_matched_as_missing_year() -> None:
    assert discover("Orchid Interview is scheduled for May 14, 27.") == ()
    assert discover("Orchid Interview is scheduled for May 14-16, 27.") == ()
    assert discover("Orchid Interview is scheduled for 14 May to 16 May 27.") == ()


def test_candidate_serialization_contains_no_source_content() -> None:
    private_marker = "SYNTHETIC-PRIVATE-MARKER"
    text = (
        f"{private_marker} Orchid Interview is scheduled for May 14, 2027 "
        "at 4:30 PM PDT."
    )
    (candidate,) = discover(text)

    serialized = repr(asdict(candidate))
    assert private_marker not in serialized
    assert "Orchid" not in serialized


def test_invalid_or_naive_provider_time_fails_closed() -> None:
    text = "Orchid Interview is scheduled for May 14, 2027."

    assert discover(text, anchor="not-a-time") == ()
    assert discover(text, anchor="2026-12-29T10:00:00") == ()
