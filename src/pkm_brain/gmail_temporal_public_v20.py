from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


PUBLIC_GMAIL_TEMPORAL_V20_FIXTURE_VERSION = (
    "gmail_temporal_public_v20_contrast_fixture_v2"
)

FixtureSelection = Literal["none", "first_uncertain", "cancelled_uncertain"]
FailClosedExpectation = Literal[
    "candidate_free",
    "conflicted_reschedule",
    "no_reschedule",
]
AbbreviatedDayExpectation = Literal["none", "resolved", "unresolved"]


@dataclass(frozen=True)
class PublicGmailTemporalV20Case:
    """One public-only contrast case with structural, never semantic, gold."""

    case_id: str
    text: str
    selection: FixtureSelection
    positive_candidate: bool = False
    selected_negative: bool = False
    advertising_negative: bool = False
    exact_group_case: bool = False
    expected_group_kind: str | None = None
    expected_group_coverage: str | None = None
    expected_group_roles: tuple[str, ...] = ()
    expected_group_reasons: tuple[str, ...] = ()
    fail_closed_expectation: FailClosedExpectation | None = None
    expected_artifact_lifecycles: tuple[str, ...] = ()
    abbreviated_day_expectation: AbbreviatedDayExpectation | None = None
    expected_abbreviated_day_surface: str | None = None
    expected_abbreviated_day_options: tuple[str, ...] = ()
    expected_abbreviated_day_blockers: tuple[str, ...] = ()
    raw_fallback_retained_expression_count: int | None = None
    expected_raw_fallback_roles: tuple[str, ...] = ()
    expected_raw_fallback_missing_roles: tuple[str, ...] = ()
    expected_raw_fallback_reasons: tuple[str, ...] = ()
    raw_source_guard_case: bool = False


def _lexical_positive(case_id: str, text: str) -> PublicGmailTemporalV20Case:
    return PublicGmailTemporalV20Case(
        case_id=case_id,
        text=text,
        selection="first_uncertain",
        positive_candidate=True,
    )


def _negative(
    case_id: str,
    text: str,
    *,
    advertising: bool = False,
) -> PublicGmailTemporalV20Case:
    return PublicGmailTemporalV20Case(
        case_id=case_id,
        text=text,
        selection="none",
        selected_negative=True,
        advertising_negative=advertising,
    )


def _reschedule(
    case_id: str,
    text: str,
    *,
    coverage: Literal["complete", "incomplete"],
    roles: tuple[str, ...],
    reasons: tuple[str, ...] = (),
    abbreviated_day_expectation: AbbreviatedDayExpectation | None = None,
) -> PublicGmailTemporalV20Case:
    return PublicGmailTemporalV20Case(
        case_id=case_id,
        text=text,
        selection="first_uncertain",
        positive_candidate=True,
        exact_group_case=True,
        expected_group_kind="reschedule",
        expected_group_coverage=coverage,
        expected_group_roles=roles,
        expected_group_reasons=reasons,
        abbreviated_day_expectation=abbreviated_day_expectation,
    )


def _ambiguous_reschedule(
    case_id: str,
    text: str,
    *,
    roles: tuple[str, ...],
    reasons: tuple[str, ...],
    abbreviated_day_expectation: AbbreviatedDayExpectation | None = None,
    abbreviated_day_surface: str | None = None,
    abbreviated_day_options: tuple[str, ...] = (),
    abbreviated_day_blockers: tuple[str, ...] = (),
    raw_fallback_retained_expression_count: int | None = None,
    raw_fallback_roles: tuple[str, ...] = (),
    raw_fallback_missing_roles: tuple[str, ...] = (),
    raw_fallback_reasons: tuple[str, ...] = (),
    raw_source_guard_case: bool = False,
) -> PublicGmailTemporalV20Case:
    return PublicGmailTemporalV20Case(
        case_id=case_id,
        text=text,
        selection="first_uncertain",
        positive_candidate=True,
        expected_group_kind="reschedule",
        expected_group_coverage="conflicted",
        expected_group_roles=roles,
        expected_group_reasons=reasons,
        fail_closed_expectation="conflicted_reschedule",
        abbreviated_day_expectation=abbreviated_day_expectation,
        expected_abbreviated_day_surface=abbreviated_day_surface,
        expected_abbreviated_day_options=abbreviated_day_options,
        expected_abbreviated_day_blockers=abbreviated_day_blockers,
        raw_fallback_retained_expression_count=(raw_fallback_retained_expression_count),
        expected_raw_fallback_roles=raw_fallback_roles,
        expected_raw_fallback_missing_roles=raw_fallback_missing_roles,
        expected_raw_fallback_reasons=raw_fallback_reasons,
        raw_source_guard_case=raw_source_guard_case,
    )


PUBLIC_GMAIL_TEMPORAL_V20_CASES = (
    # Six bounded lexical additions. Every resulting candidate remains review-only.
    _lexical_positive(
        "lex_policy_effective_date",
        "The policy effective date is August 1, 2027.",
    ),
    _lexical_positive(
        "lex_applications_open",
        "Applications open August 12, 2027.",
    ),
    _lexical_positive(
        "lex_policy_in_force",
        "The policy is in force beginning August 1, 2027.",
    ),
    _lexical_positive(
        "lex_enrollment_begins",
        "Enrollment begins August 12, 2027.",
    ),
    _lexical_positive(
        "lex_registration_open_from",
        "Registration is open from August 12, 2027.",
    ),
    _lexical_positive(
        "lex_rule_applies_as_of",
        "The rule applies as of August 1, 2027.",
    ),
    _lexical_positive(
        "lex_applications_are_open_from",
        "Applications are open from August 12, 2027.",
    ),
    _lexical_positive(
        "lex_applications_will_be_open_from",
        "Applications will be open from August 12, 2027.",
    ),
    _lexical_positive(
        "lex_applications_remain_open_from",
        "Applications remain open from August 12, 2027.",
    ),
    _lexical_positive(
        "lex_enrollment_starts",
        "Enrollment starts August 12, 2027.",
    ),
    # Matched object, adjectival, generic, advertising, and P1 safety contrasts.
    _negative(
        "neg_keep_applications_open",
        "Keep applications open on August 12, 2027.",
    ),
    _negative(
        "neg_reviewed_applications_open",
        "We reviewed applications open on August 12, 2027.",
    ),
    _negative(
        "neg_use_applications_open",
        "I use applications open on August 12, 2027 for QA.",
    ),
    _negative(
        "neg_registration_cross_segment",
        "Registration is open\n\n          from August 12, 2027.",
    ),
    _negative(
        "neg_open_report_object",
        "Please open the August 12, 2027 report.",
    ),
    _negative(
        "neg_application_begins_with",
        "The application begins with an August 12, 2027 questionnaire.",
    ),
    _negative(
        "neg_store_open_from",
        "The store is open from August 12, 2027.",
    ),
    _negative(
        "neg_discount_applies_as_of",
        "The discount applies as of August 1, 2027.",
    ),
    _negative(
        "neg_report_in_force",
        "The report is in force beginning August 1, 2027.",
    ),
    _negative(
        "neg_subjectless_effective_date",
        "The effective date is August 1, 2027.",
    ),
    _negative(
        "neg_advertising_applications_open",
        "Advertisement: Applications open August 12, 2027. Save 25%. Unsubscribe.",
        advertising=True,
    ),
    _negative(
        "neg_stores_are_open_from",
        "Stores are open from August 12, 2027.",
    ),
    _negative(
        "neg_report_will_be_open_from",
        "The report will be open from August 12, 2027.",
    ),
    _negative(
        "neg_application_starts_with_questionnaire",
        "The application starts with questionnaire on August 12, 2027.",
    ),
    _negative(
        "neg_applications_are_open_for_qa",
        "Applications are open for QA on August 12, 2027.",
    ),
    _negative(
        "neg_applications_are_open_cross_segment",
        "Applications are open\n\n          from August 12, 2027.",
    ),
    _negative(
        "neg_advertising_applications_will_be_open_from",
        (
            "Advertisement: Applications will be open from August 12, 2027. "
            "Save 25%. Unsubscribe."
        ),
        advertising=True,
    ),
    # Complete and source-incomplete directional reschedule structures.
    _reschedule(
        "reschedule_direct",
        "The meeting was rescheduled from Aug 12 to Aug 15.",
        coverage="complete",
        roles=("rescheduled_old", "rescheduled_replacement"),
    ),
    _reschedule(
        "reschedule_pushed_back",
        "The meeting was pushed back from Aug 12 until Aug 15.",
        coverage="complete",
        roles=("rescheduled_old", "rescheduled_replacement"),
    ),
    _reschedule(
        "reschedule_inverse",
        "The meeting moved to Aug 15 from Aug 12.",
        coverage="complete",
        roles=("rescheduled_replacement", "rescheduled_old"),
    ),
    _reschedule(
        "reschedule_new_date",
        "Meeting update — New date: Aug 15 (was Aug 12).",
        coverage="complete",
        roles=("rescheduled_replacement", "rescheduled_old"),
    ),
    _reschedule(
        "reschedule_now_instead",
        "The meeting is now Aug 15 instead of Aug 12.",
        coverage="complete",
        roles=("rescheduled_replacement", "rescheduled_old"),
    ),
    _reschedule(
        "reschedule_arrow_forward",
        "The meeting moved Aug 12 -> Aug 15.",
        coverage="complete",
        roles=("rescheduled_old", "rescheduled_replacement"),
    ),
    _reschedule(
        "reschedule_arrow_reverse",
        "The meeting moved Aug 15 <- Aug 12.",
        coverage="complete",
        roles=("rescheduled_replacement", "rescheduled_old"),
    ),
    _reschedule(
        "reschedule_replacement_postponed",
        "The meeting was postponed until Aug 15.",
        coverage="incomplete",
        roles=("rescheduled_replacement",),
        reasons=("rescheduled_old_missing_from_source",),
    ),
    _reschedule(
        "reschedule_replacement_moved",
        "The meeting moved to Aug 15.",
        coverage="incomplete",
        roles=("rescheduled_replacement",),
        reasons=("rescheduled_old_missing_from_source",),
    ),
    # Every endpoint-alternative form remains one conflicted, non-authorizing group.
    _ambiguous_reschedule(
        "ambiguous_new_date_options",
        "Meeting update — New date: Aug 15 (was Aug 12 or Aug 13).",
        roles=("rescheduled_replacement", "unresolved", "unresolved"),
        reasons=("reschedule_endpoint_alternatives_unresolved",),
    ),
    _ambiguous_reschedule(
        "ambiguous_now_instead_options",
        "The meeting is now Aug 15 instead of Aug 12 or Aug 13.",
        roles=("rescheduled_replacement", "unresolved", "unresolved"),
        reasons=("reschedule_endpoint_alternatives_unresolved",),
    ),
    _ambiguous_reschedule(
        "ambiguous_arrow_replacements",
        "The meeting moved Aug 12 -> Aug 15 or Aug 16.",
        roles=("rescheduled_old", "unresolved", "unresolved"),
        reasons=("reschedule_endpoint_alternatives_unresolved",),
    ),
    _ambiguous_reschedule(
        "ambiguous_direct_replacements",
        "The meeting was rescheduled from Aug 12 to Aug 15 or Aug 16.",
        roles=("rescheduled_old", "unresolved", "unresolved"),
        reasons=("reschedule_endpoint_alternatives_unresolved",),
    ),
    _ambiguous_reschedule(
        "ambiguous_connector_or_on",
        "The meeting was rescheduled from Aug 12 to Aug 15 or on Aug 16.",
        roles=("rescheduled_old", "unresolved", "unresolved"),
        reasons=("reschedule_endpoint_alternatives_unresolved",),
    ),
    _ambiguous_reschedule(
        "ambiguous_connector_or_at",
        "The meeting was rescheduled from Aug 12 to Aug 15 or at Aug 16.",
        roles=("rescheduled_old", "unresolved", "unresolved"),
        reasons=("reschedule_endpoint_alternatives_unresolved",),
    ),
    _ambiguous_reschedule(
        "ambiguous_connector_or_perhaps",
        "The meeting was rescheduled from Aug 12 to Aug 15, or perhaps Aug 16.",
        roles=("rescheduled_old", "unresolved", "unresolved"),
        reasons=("reschedule_endpoint_alternatives_unresolved",),
    ),
    _ambiguous_reschedule(
        "ambiguous_connector_or_maybe",
        "The meeting was rescheduled from Aug 12 to Aug 15 or maybe Aug 16.",
        roles=("rescheduled_old", "unresolved", "unresolved"),
        reasons=("reschedule_endpoint_alternatives_unresolved",),
    ),
    _ambiguous_reschedule(
        "ambiguous_connector_or_maybe_at",
        "The meeting was rescheduled from Aug 12 to Aug 15 or maybe at Aug 16.",
        roles=("rescheduled_old", "unresolved", "unresolved"),
        reasons=("reschedule_endpoint_alternatives_unresolved",),
    ),
    # Generic hedges are not accepted as directional ``or`` grammar. They
    # quarantine the entire bounded reschedule rather than preserving either
    # endpoint as authoritative.
    _ambiguous_reschedule(
        "ambiguous_connector_or_possibly",
        "The meeting was rescheduled from Aug 12 to Aug 15 or possibly Aug 16.",
        roles=("unresolved", "unresolved", "unresolved"),
        reasons=("reschedule_endpoint_connector_unresolved",),
    ),
    _ambiguous_reschedule(
        "ambiguous_connector_or_potentially",
        "The meeting was rescheduled from Aug 12 to Aug 15 or potentially Aug 16.",
        roles=("unresolved", "unresolved", "unresolved"),
        reasons=("reschedule_endpoint_connector_unresolved",),
    ),
    _ambiguous_reschedule(
        "ambiguous_connector_or_conceivably",
        "The meeting was rescheduled from Aug 12 to Aug 15 or conceivably Aug 16.",
        roles=("unresolved", "unresolved", "unresolved"),
        reasons=("reschedule_endpoint_connector_unresolved",),
    ),
    _ambiguous_reschedule(
        "ambiguous_inverse_old_options",
        "The meeting moved to Aug 15 from Aug 12 or Aug 13.",
        roles=("rescheduled_replacement", "unresolved", "unresolved"),
        reasons=("reschedule_endpoint_alternatives_unresolved",),
    ),
    _ambiguous_reschedule(
        "ambiguous_three_replacements",
        "The meeting was rescheduled from Aug 12 to Aug 15, Aug 16, or Aug 17.",
        roles=(
            "rescheduled_old",
            "unresolved",
            "unresolved",
            "unresolved",
        ),
        reasons=("reschedule_endpoint_alternatives_unresolved",),
    ),
    _ambiguous_reschedule(
        "ambiguous_replacement_only_postponed_options",
        "The meeting was postponed until Aug 15 or Aug 16.",
        roles=("unresolved", "unresolved"),
        reasons=(
            "reschedule_endpoint_alternatives_unresolved",
            "rescheduled_old_missing_from_source",
        ),
    ),
    _ambiguous_reschedule(
        "ambiguous_replacement_only_new_date_options",
        "Meeting update — New date: Aug 15 or Aug 16.",
        roles=("unresolved", "unresolved"),
        reasons=(
            "reschedule_endpoint_alternatives_unresolved",
            "rescheduled_old_missing_from_source",
        ),
    ),
    _ambiguous_reschedule(
        "ambiguous_replacement_only_three_options",
        "The meeting was rescheduled for Aug 15, Aug 16, or Aug 17.",
        roles=("unresolved", "unresolved", "unresolved"),
        reasons=(
            "reschedule_endpoint_alternatives_unresolved",
            "rescheduled_old_missing_from_source",
        ),
    ),
    _ambiguous_reschedule(
        "ambiguous_old_only_options",
        "The meeting was rescheduled from Aug 12 or Aug 13.",
        roles=("unresolved", "unresolved"),
        reasons=(
            "reschedule_endpoint_alternatives_unresolved",
            "rescheduled_replacement_missing_from_source",
        ),
    ),
    _ambiguous_reschedule(
        "ambiguous_leading_inverse_replacements",
        "The meeting was rescheduled to Aug 15 or Aug 16 from Aug 12.",
        roles=("unresolved", "unresolved", "rescheduled_old"),
        reasons=("reschedule_endpoint_alternatives_unresolved",),
    ),
    _ambiguous_reschedule(
        "ambiguous_leading_now_replacements",
        "The meeting is now Aug 15 or Aug 16 instead of Aug 12.",
        roles=("unresolved", "unresolved", "rescheduled_old"),
        reasons=("reschedule_endpoint_alternatives_unresolved",),
    ),
    _ambiguous_reschedule(
        "ambiguous_leading_arrow_olds",
        "The meeting moved Aug 12 or Aug 13 -> Aug 15.",
        roles=("unresolved", "unresolved", "rescheduled_replacement"),
        reasons=("reschedule_endpoint_alternatives_unresolved",),
    ),
    _ambiguous_reschedule(
        "ambiguous_leading_new_date_replacements",
        "Meeting update — New date: Aug 15 or Aug 16 (was Aug 12).",
        roles=("unresolved", "unresolved", "rescheduled_old"),
        reasons=("reschedule_endpoint_alternatives_unresolved",),
    ),
    _ambiguous_reschedule(
        "ambiguous_collapsed_old_slot",
        "The meeting was rescheduled from Aug 12 or Aug 13 to Aug 15.",
        roles=("unresolved", "unresolved"),
        reasons=(
            "reschedule_endpoint_alternatives_unresolved",
            "reschedule_endpoint_representation_unresolved",
        ),
    ),
    _ambiguous_reschedule(
        "ambiguous_both_endpoint_slots",
        "The meeting was moved to Aug 15 or Aug 16 from Aug 12 or Aug 13.",
        roles=("unresolved", "unresolved", "unresolved", "unresolved"),
        reasons=("reschedule_endpoint_alternatives_unresolved",),
    ),
    _ambiguous_reschedule(
        "ambiguous_collapsed_both_slots",
        "The meeting was rescheduled from Aug 12 or Aug 13 to Aug 15 or Aug 16.",
        roles=("unresolved", "unresolved", "unresolved"),
        reasons=(
            "reschedule_endpoint_alternatives_unresolved",
            "reschedule_endpoint_representation_unresolved",
        ),
    ),
    _ambiguous_reschedule(
        "ambiguous_bidirectional_arrow",
        "The meeting moved Aug 12 <-> Aug 15.",
        roles=("unresolved", "unresolved"),
        reasons=("reschedule_endpoint_direction_unresolved",),
    ),
    _ambiguous_reschedule(
        "ambiguous_collapsed_range",
        "The meeting was pushed back from Aug 12 until 15.",
        roles=("unresolved",),
        reasons=("reschedule_endpoint_representation_unresolved",),
    ),
    # A bare day inherits month/year only inside a strict reschedule endpoint.
    # The inherited date is exact inventory, but every alternative remains
    # conflicted and review-only.
    _ambiguous_reschedule(
        "abbreviated_direct_or_day",
        ("The meeting was rescheduled from August 12, 2027 to August 15, 2027 or 16."),
        roles=("rescheduled_old", "unresolved", "unresolved"),
        reasons=("reschedule_endpoint_alternatives_unresolved",),
        abbreviated_day_expectation="resolved",
        abbreviated_day_surface="16",
        abbreviated_day_options=("2027-08-16",),
        abbreviated_day_blockers=("reschedule_endpoint_alternatives_unresolved",),
        raw_fallback_retained_expression_count=2,
        raw_fallback_roles=("unresolved", "unresolved"),
        raw_fallback_reasons=(
            "reschedule_endpoint_abbreviated_alternative_unresolved",
        ),
    ),
    _ambiguous_reschedule(
        "abbreviated_direct_slash_day",
        ("The meeting was rescheduled from August 12, 2027 to August 15, 2027/16."),
        roles=("rescheduled_old", "unresolved", "unresolved"),
        reasons=("reschedule_endpoint_alternatives_unresolved",),
        abbreviated_day_expectation="resolved",
        abbreviated_day_surface="16",
        abbreviated_day_options=("2027-08-16",),
        abbreviated_day_blockers=("reschedule_endpoint_alternatives_unresolved",),
    ),
    _ambiguous_reschedule(
        "abbreviated_replacement_only_or_day",
        "The meeting was postponed until August 15, 2027 or 16.",
        roles=("unresolved", "unresolved"),
        reasons=(
            "reschedule_endpoint_alternatives_unresolved",
            "rescheduled_old_missing_from_source",
        ),
        abbreviated_day_expectation="resolved",
        abbreviated_day_surface="16",
        abbreviated_day_options=("2027-08-16",),
        abbreviated_day_blockers=("reschedule_endpoint_alternatives_unresolved",),
    ),
    _ambiguous_reschedule(
        "abbreviated_replacement_only_slash_day",
        "The meeting was postponed until August 15, 2027/16.",
        roles=("unresolved", "unresolved"),
        reasons=(
            "reschedule_endpoint_alternatives_unresolved",
            "rescheduled_old_missing_from_source",
        ),
        abbreviated_day_expectation="resolved",
        abbreviated_day_surface="16",
        abbreviated_day_options=("2027-08-16",),
        abbreviated_day_blockers=("reschedule_endpoint_alternatives_unresolved",),
        raw_fallback_retained_expression_count=1,
        raw_fallback_roles=("unresolved",),
        raw_fallback_missing_roles=("rescheduled_old",),
        raw_fallback_reasons=(
            "reschedule_endpoint_abbreviated_alternative_unresolved",
        ),
    ),
    _ambiguous_reschedule(
        "abbreviated_inverse_or_day",
        ("The meeting was moved to August 15, 2027 or 16 from August 12, 2027."),
        roles=("unresolved", "unresolved", "rescheduled_old"),
        reasons=("reschedule_endpoint_alternatives_unresolved",),
        abbreviated_day_expectation="resolved",
        abbreviated_day_surface="16",
        abbreviated_day_options=("2027-08-16",),
        abbreviated_day_blockers=("reschedule_endpoint_alternatives_unresolved",),
    ),
    _ambiguous_reschedule(
        "abbreviated_inverse_slash_day",
        ("The meeting was moved to August 15, 2027/16 from August 12, 2027."),
        roles=("unresolved", "unresolved", "rescheduled_old"),
        reasons=("reschedule_endpoint_alternatives_unresolved",),
        abbreviated_day_expectation="resolved",
        abbreviated_day_surface="16",
        abbreviated_day_options=("2027-08-16",),
        abbreviated_day_blockers=("reschedule_endpoint_alternatives_unresolved",),
    ),
    _ambiguous_reschedule(
        "abbreviated_old_slot_or_day",
        ("The meeting was rescheduled from August 12, 2027 or 13 to August 15, 2027."),
        roles=("unresolved", "unresolved", "rescheduled_replacement"),
        reasons=("reschedule_endpoint_alternatives_unresolved",),
        abbreviated_day_expectation="resolved",
        abbreviated_day_surface="13",
        abbreviated_day_options=("2027-08-13",),
        abbreviated_day_blockers=("reschedule_endpoint_alternatives_unresolved",),
    ),
    _ambiguous_reschedule(
        "abbreviated_old_slot_slash_day",
        ("The meeting was rescheduled from August 12, 2027/13 to August 15, 2027."),
        roles=("unresolved", "unresolved", "rescheduled_replacement"),
        reasons=("reschedule_endpoint_alternatives_unresolved",),
        abbreviated_day_expectation="resolved",
        abbreviated_day_surface="13",
        abbreviated_day_options=("2027-08-13",),
        abbreviated_day_blockers=("reschedule_endpoint_alternatives_unresolved",),
    ),
    _ambiguous_reschedule(
        "abbreviated_ordinal_day",
        "The meeting was postponed until August 15, 2027 or 16th.",
        roles=("unresolved", "unresolved"),
        reasons=(
            "reschedule_endpoint_alternatives_unresolved",
            "rescheduled_old_missing_from_source",
        ),
        abbreviated_day_expectation="resolved",
        abbreviated_day_surface="16th",
        abbreviated_day_options=("2027-08-16",),
        abbreviated_day_blockers=("reschedule_endpoint_alternatives_unresolved",),
    ),
    _ambiguous_reschedule(
        "abbreviated_day_first",
        ("The meeting was rescheduled from 12 August 2027 to 15 August 2027 or 16."),
        roles=("rescheduled_old", "unresolved", "unresolved"),
        reasons=("reschedule_endpoint_alternatives_unresolved",),
        abbreviated_day_expectation="resolved",
        abbreviated_day_surface="16",
        abbreviated_day_options=("2027-08-16",),
        abbreviated_day_blockers=("reschedule_endpoint_alternatives_unresolved",),
    ),
    _ambiguous_reschedule(
        "abbreviated_invalid_day_unresolved",
        ("The meeting was rescheduled from April 29, 2027 to April 30, 2027 or 31."),
        roles=("rescheduled_old", "unresolved", "unresolved"),
        reasons=("reschedule_endpoint_alternatives_unresolved",),
        abbreviated_day_expectation="unresolved",
        abbreviated_day_surface="31",
        abbreviated_day_blockers=(
            "reschedule_endpoint_alternatives_unresolved",
            "invalid_calendar_date",
        ),
    ),
    _ambiguous_reschedule(
        "abbreviated_invalid_slash_day_unresolved",
        ("The meeting was rescheduled from August 12, 2027 to August 15, 2027/32."),
        roles=("unresolved", "unresolved", "unresolved"),
        reasons=("reschedule_endpoint_connector_unresolved",),
        abbreviated_day_expectation="unresolved",
        abbreviated_day_surface="32",
        abbreviated_day_blockers=(
            "reschedule_endpoint_alternatives_unresolved",
            "invalid_calendar_date",
        ),
    ),
    # A second endpoint can remain legible in source even when the expression
    # parser deliberately declines to normalize it. The raw-source structural
    # guard must still quarantine the bounded reschedule.
    _ambiguous_reschedule(
        "abbreviated_raw_direct_ordinal_article",
        (
            "The meeting was rescheduled from August 12, 2027 to "
            "August 15, 2027 or the 16th."
        ),
        roles=("unresolved", "unresolved"),
        reasons=("reschedule_endpoint_abbreviated_alternative_unresolved",),
        abbreviated_day_expectation="none",
        raw_source_guard_case=True,
    ),
    _ambiguous_reschedule(
        "abbreviated_raw_direct_shared_trailing_year",
        ("The meeting was rescheduled from August 12, 2027 to August 15 or 16, 2027."),
        roles=("unresolved", "unresolved"),
        reasons=("reschedule_endpoint_abbreviated_alternative_unresolved",),
        abbreviated_day_expectation="none",
        raw_source_guard_case=True,
    ),
    _ambiguous_reschedule(
        "abbreviated_raw_replacement_only_ordinal_article",
        "The meeting was postponed until August 15, 2027 or the 16th.",
        roles=("unresolved",),
        reasons=(
            "reschedule_endpoint_abbreviated_alternative_unresolved",
            "rescheduled_old_missing_from_source",
        ),
        abbreviated_day_expectation="none",
        raw_source_guard_case=True,
    ),
    _ambiguous_reschedule(
        "abbreviated_raw_inverse_ordinal_article",
        ("The meeting was moved to August 15, 2027 or the 16th from August 12, 2027."),
        roles=("unresolved", "unresolved"),
        reasons=("reschedule_endpoint_abbreviated_alternative_unresolved",),
        abbreviated_day_expectation="none",
        raw_source_guard_case=True,
    ),
    _ambiguous_reschedule(
        "abbreviated_raw_old_slot_ordinal_article",
        (
            "The meeting was rescheduled from August 12, 2027 or the 13th to "
            "August 15, 2027."
        ),
        roles=("unresolved", "unresolved"),
        reasons=("reschedule_endpoint_abbreviated_alternative_unresolved",),
        abbreviated_day_expectation="none",
        raw_source_guard_case=True,
    ),
    # These are negatives only for the abbreviated shared-month parser. Keep
    # their ordinary candidate expectations separate from semantic negatives.
    PublicGmailTemporalV20Case(
        case_id="abbreviated_negative_prose",
        text="Vacation is August 15, 2027 or 16.",
        selection="none",
        abbreviated_day_expectation="none",
    ),
    _reschedule(
        "abbreviated_negative_count",
        (
            "The meeting was rescheduled from August 12, 2027 to "
            "August 15, 2027 or 16 people joined the waitlist."
        ),
        coverage="complete",
        roles=("rescheduled_old", "rescheduled_replacement"),
        abbreviated_day_expectation="none",
    ),
    _ambiguous_reschedule(
        "abbreviated_negative_full_date_slash",
        (
            "The meeting was rescheduled from August 12, 2027 to "
            "August 15, 2027 / August 16, 2027."
        ),
        roles=("unresolved", "unresolved", "unresolved"),
        reasons=("reschedule_endpoint_connector_unresolved",),
        abbreviated_day_expectation="none",
    ),
    PublicGmailTemporalV20Case(
        case_id="ambiguous_unrelated_interval",
        text="The meeting was postponed and vacation runs from Aug 12 until Aug 15.",
        selection="first_uncertain",
        positive_candidate=True,
        fail_closed_expectation="no_reschedule",
    ),
    PublicGmailTemporalV20Case(
        case_id="ambiguous_location_move",
        text="The meeting moved to Zoom and runs from Aug 12 until Aug 15.",
        selection="first_uncertain",
        positive_candidate=True,
        fail_closed_expectation="no_reschedule",
    ),
    # Cancellation is represented only when a current temporal endpoint exists.
    PublicGmailTemporalV20Case(
        case_id="lifecycle_cancelled_exact",
        text="The dentist appointment scheduled for August 14, 2027 was cancelled.",
        selection="cancelled_uncertain",
        positive_candidate=True,
        expected_artifact_lifecycles=("cancelled",),
    ),
    PublicGmailTemporalV20Case(
        case_id="lifecycle_cancelled_then_scheduled",
        text=(
            "The meeting was cancelled on August 14, 2027 after being scheduled "
            "for August 16, 2027."
        ),
        selection="first_uncertain",
        positive_candidate=True,
        expected_artifact_lifecycles=("cancelled", "scheduled"),
    ),
    PublicGmailTemporalV20Case(
        case_id="lifecycle_split_events",
        text=(
            "Alpha meeting was cancelled August 14, 2027. "
            "Beta workshop was scheduled August 16, 2027."
        ),
        selection="first_uncertain",
        positive_candidate=True,
        expected_artifact_lifecycles=("cancelled", "scheduled"),
    ),
    PublicGmailTemporalV20Case(
        case_id="lifecycle_date_free_cancellation",
        text="Subject: Update\n\nIt was cancelled.",
        selection="none",
        fail_closed_expectation="candidate_free",
    ),
    PublicGmailTemporalV20Case(
        case_id="lifecycle_unrelated_reservation",
        text=(
            "The meeting was cancelled. "
            "The hotel reservation was booked for August 15, 2027."
        ),
        selection="first_uncertain",
        positive_candidate=True,
        expected_artifact_lifecycles=("scheduled",),
    ),
)
