from __future__ import annotations

from typing import Any, Literal


GoldGrade = Literal["exact", "partial", "absent"]
GoldVerdict = Literal["supported", "uncertain"]


def _candidate(
    expression: str,
    subject: str,
    *,
    expression_form: str = "explicit_date",
    expression_field: str = "message",
    subject_type: str = "event",
    subject_field: str | None = None,
    lifecycle_surface: str | None = None,
    lifecycle_role: str | None = None,
    lifecycle_field: str | None = None,
    relation: str,
    kind: str,
    lifecycle: str,
    normalized_value: str | None = None,
    requires_defer: bool,
    quality: Literal["exact", "partial"] = "exact",
) -> dict[str, Any]:
    if (lifecycle_surface is None) != (lifecycle_role is None):
        raise ValueError("lifecycle surface and role must be supplied together")
    subject_field = subject_field or expression_field
    lifecycle_locator: dict[str, Any] | None = None
    if lifecycle_surface is not None and lifecycle_role is not None:
        lifecycle_locator = {
            "surface": lifecycle_surface,
            "lifecycle_role": lifecycle_role,
            "field": lifecycle_field or expression_field,
        }
    return {
        "quality": quality,
        "locator": {
            "expression": {
                "surface": expression,
                "form": expression_form,
                "field": expression_field,
            },
            "subject": {
                "surface": subject,
                "mention_type": subject_type,
                "field": subject_field,
            },
            "lifecycle_mention": lifecycle_locator,
            "derived": {
                "relation": relation,
                "kind": kind,
                "lifecycle": lifecycle,
                "normalized_value": normalized_value,
                "requires_defer": requires_defer,
            },
        },
    }


def _member(
    member_id: str,
    expected_verdict: GoldVerdict,
    *alternatives: dict[str, Any],
    baseline_frontier_grade: GoldGrade = "exact",
) -> dict[str, Any]:
    calibrated_alternatives = []
    for alternative in alternatives:
        calibrated = dict(alternative)
        calibrated["expected_verdict"] = (
            "uncertain" if calibrated.get("quality") == "partial" else expected_verdict
        )
        calibrated_alternatives.append(calibrated)
    return {
        "member_id": member_id,
        "expected_verdict": expected_verdict,
        "baseline_frontier_grade": baseline_frontier_grade,
        "alternatives": calibrated_alternatives,
    }


def _unit(
    unit_id: str,
    truth: str,
    *members: dict[str, Any],
    baseline_frontier_grade: GoldGrade = "exact",
) -> dict[str, Any]:
    return {
        "unit_id": unit_id,
        "truth": truth,
        "baseline_frontier_grade": baseline_frontier_grade,
        "members": list(members),
    }


def _scheduled(
    expression: str,
    subject: str,
    *,
    expression_form: str = "explicit_date",
    expression_field: str = "message",
    subject_field: str | None = None,
    normalized_value: str | None,
    requires_defer: bool,
    quality: Literal["exact", "partial"] = "exact",
) -> dict[str, Any]:
    return _candidate(
        expression,
        subject,
        expression_form=expression_form,
        expression_field=expression_field,
        subject_field=subject_field,
        lifecycle_surface="scheduled",
        lifecycle_role="scheduled",
        lifecycle_field=expression_field,
        relation="occurrence",
        kind="planned",
        lifecycle="scheduled",
        normalized_value=normalized_value,
        requires_defer=requires_defer,
        quality=quality,
    )


def _occurrence_without_lifecycle(
    expression: str,
    subject: str,
    *,
    expression_form: str = "explicit_date",
    expression_field: str = "message",
    subject_type: str = "event",
    subject_field: str | None = None,
    kind: str,
    normalized_value: str | None,
    requires_defer: bool,
    quality: Literal["exact", "partial"] = "partial",
) -> dict[str, Any]:
    return _candidate(
        expression,
        subject,
        expression_form=expression_form,
        expression_field=expression_field,
        subject_type=subject_type,
        subject_field=subject_field,
        relation="occurrence",
        kind=kind,
        lifecycle="none",
        normalized_value=normalized_value,
        requires_defer=requires_defer,
        quality=quality,
    )


SEMANTIC_GOLD: dict[str, list[dict[str, Any]]] = {
    "syn_clear_01": [
        _unit(
            "nimbus_interview_schedule",
            "Nimbus Interview is scheduled for 2027-08-14 16:30 -07:00.",
            _member(
                "schedule",
                "supported",
                _scheduled(
                    "August 14, 2027 at 16:30 -07:00",
                    "Interview",
                    expression_form="date_time",
                    expression_field="body",
                    subject_field="body",
                    normalized_value="2027-08-14T16:30:00-07:00",
                    requires_defer=False,
                ),
                _occurrence_without_lifecycle(
                    "August 14, 2027 at 16:30 -07:00",
                    "Interview",
                    expression_form="date_time",
                    expression_field="body",
                    subject_field="body",
                    kind="planned",
                    normalized_value="2027-08-14T16:30:00-07:00",
                    requires_defer=False,
                ),
                _occurrence_without_lifecycle(
                    "August 14, 2027 at 16:30 -07:00",
                    "Nimbus Interview",
                    expression_form="date_time",
                    expression_field="body",
                    subject_type="event_title_candidate",
                    subject_field="subject",
                    kind="planned",
                    normalized_value="2027-08-14T16:30:00-07:00",
                    requires_defer=True,
                ),
                _occurrence_without_lifecycle(
                    "August 14, 2027 at 16:30 -07:00",
                    "Interview",
                    expression_form="date_time",
                    expression_field="body",
                    subject_field="subject",
                    kind="planned",
                    normalized_value="2027-08-14T16:30:00-07:00",
                    requires_defer=True,
                ),
            ),
        )
    ],
    "syn_clear_02": [
        _unit(
            "dental_appointment_schedule",
            "The dental appointment is scheduled for 2027-09-02.",
            _member(
                "schedule",
                "supported",
                _scheduled(
                    "September 2, 2027",
                    "appointment",
                    normalized_value="2027-09-02",
                    requires_defer=False,
                ),
                _occurrence_without_lifecycle(
                    "September 2, 2027",
                    "appointment",
                    kind="planned",
                    normalized_value="2027-09-02",
                    requires_defer=False,
                ),
            ),
        )
    ],
    "syn_clear_03": [
        _unit(
            "fellowship_application_deadline",
            "The fellowship application must be submitted by 2027-08-20.",
            _member(
                "deadline",
                "supported",
                _candidate(
                    "August 20, 2027",
                    "submit",
                    subject_type="action",
                    relation="deadline",
                    kind="planned",
                    lifecycle="none",
                    normalized_value="2027-08-20",
                    requires_defer=False,
                ),
            ),
        )
    ],
    "syn_clear_04": [
        _unit(
            "atlas_meeting_occurrence",
            "The Atlas planning meeting occurred on 2027-08-11.",
            _member(
                "occurrence",
                "supported",
                _occurrence_without_lifecycle(
                    "August 11, 2027",
                    "meeting",
                    kind="actual",
                    normalized_value="2027-08-11",
                    requires_defer=False,
                    quality="exact",
                ),
            ),
        )
    ],
    "syn_clear_05": [
        _unit(
            "cedar_forum_schedule",
            "Cedar Leadership Forum occurs on 2027-08-18.",
            _member(
                "schedule",
                "supported",
                _occurrence_without_lifecycle(
                    "August 18, 2027",
                    "Cedar Leadership Forum",
                    expression_field="body",
                    subject_type="event_title_candidate",
                    subject_field="subject",
                    kind="unspecified",
                    normalized_value="2027-08-18",
                    requires_defer=True,
                    quality="exact",
                ),
                _occurrence_without_lifecycle(
                    "August 18, 2027",
                    "Forum",
                    expression_field="body",
                    subject_field="subject",
                    kind="unspecified",
                    normalized_value="2027-08-18",
                    requires_defer=True,
                ),
            ),
        )
    ],
    "syn_clear_06": [
        _unit(
            "project_review_tomorrow_morning",
            "The project review is tomorrow morning.",
            _member(
                "occurrence",
                "supported",
                _occurrence_without_lifecycle(
                    "tomorrow morning",
                    "review",
                    expression_form="coarse_relative",
                    kind="unspecified",
                    normalized_value="2027-08-11",
                    requires_defer=True,
                    quality="exact",
                ),
            ),
        )
    ],
    "syn_clear_07": [
        _unit(
            "alpha_meeting_schedule",
            "Alpha meeting is scheduled for 2027-08-14.",
            _member(
                "schedule",
                "supported",
                _scheduled(
                    "August 14, 2027",
                    "meeting",
                    normalized_value="2027-08-14",
                    requires_defer=False,
                ),
                _occurrence_without_lifecycle(
                    "August 14, 2027",
                    "meeting",
                    kind="planned",
                    normalized_value="2027-08-14",
                    requires_defer=False,
                ),
            ),
        ),
        _unit(
            "beta_workshop_schedule",
            "Beta workshop is scheduled for 2027-08-16.",
            _member(
                "schedule",
                "supported",
                _scheduled(
                    "August 16, 2027",
                    "workshop",
                    normalized_value="2027-08-16",
                    requires_defer=False,
                ),
                _occurrence_without_lifecycle(
                    "August 16, 2027",
                    "workshop",
                    kind="planned",
                    normalized_value="2027-08-16",
                    requires_defer=False,
                ),
            ),
        ),
    ],
    "syn_clear_08": [
        _unit(
            "board_packet_deadline",
            "The board packet must be sent by 2027-08-22.",
            _member(
                "deadline",
                "supported",
                _candidate(
                    "August 22, 2027",
                    "send",
                    subject_type="action",
                    relation="deadline",
                    kind="planned",
                    lifecycle="none",
                    normalized_value="2027-08-22",
                    requires_defer=False,
                ),
            ),
        )
    ],
    "syn_lifecycle_01": [
        _unit(
            "dentist_appointment_cancelled",
            "The dentist appointment scheduled for 2027-08-14 was cancelled.",
            _member(
                "cancelled",
                "supported",
                _candidate(
                    "August 14, 2027",
                    "appointment",
                    lifecycle_surface="cancelled",
                    lifecycle_role="cancelled",
                    relation="occurrence",
                    kind="planned",
                    lifecycle="cancelled",
                    normalized_value="2027-08-14",
                    requires_defer=False,
                ),
            ),
        )
    ],
    "syn_lifecycle_02": [
        _unit(
            "planning_workshop_completed",
            "The 2027-08-12 planning workshop was completed.",
            _member(
                "completed",
                "supported",
                _candidate(
                    "August 12, 2027",
                    "workshop",
                    lifecycle_surface="completed",
                    lifecycle_role="completed",
                    relation="unspecified",
                    kind="unspecified",
                    lifecycle="completed",
                    normalized_value="2027-08-12",
                    requires_defer=False,
                ),
            ),
        )
    ],
    "syn_lifecycle_03": [
        _unit(
            "hiring_interview_reschedule",
            "The hiring interview was rescheduled from 2027-08-14 to 2027-08-16.",
            _member(
                "old_endpoint",
                "uncertain",
                _candidate(
                    "August 14, 2027",
                    "interview",
                    lifecycle_surface="rescheduled",
                    lifecycle_role="rescheduled",
                    relation="occurrence",
                    kind="planned",
                    lifecycle="rescheduled_old",
                    normalized_value="2027-08-14",
                    requires_defer=False,
                ),
                _candidate(
                    "August 14, 2027",
                    "interview",
                    lifecycle_surface="rescheduled",
                    lifecycle_role="rescheduled",
                    relation="unspecified",
                    kind="unspecified",
                    lifecycle="unknown",
                    normalized_value="2027-08-14",
                    requires_defer=True,
                    quality="partial",
                ),
                baseline_frontier_grade="exact",
            ),
            _member(
                "replacement_endpoint",
                "uncertain",
                _candidate(
                    "August 16, 2027",
                    "interview",
                    lifecycle_surface="rescheduled",
                    lifecycle_role="rescheduled",
                    relation="occurrence",
                    kind="planned",
                    lifecycle="rescheduled_replacement",
                    normalized_value="2027-08-16",
                    requires_defer=False,
                ),
                _candidate(
                    "August 16, 2027",
                    "interview",
                    lifecycle_surface="rescheduled",
                    lifecycle_role="rescheduled",
                    relation="unspecified",
                    kind="unspecified",
                    lifecycle="unknown",
                    normalized_value="2027-08-16",
                    requires_defer=True,
                    quality="partial",
                ),
                baseline_frontier_grade="exact",
            ),
            baseline_frontier_grade="exact",
        )
    ],
    "syn_lifecycle_04": [
        _unit(
            "application_deadline_extension",
            "The application deadline was extended to 2027-08-25.",
            _member(
                "new_deadline",
                "supported",
                _candidate(
                    "August 25, 2027",
                    "deadline",
                    subject_type="deadline",
                    relation="deadline",
                    kind="planned",
                    lifecycle="none",
                    normalized_value="2027-08-25",
                    requires_defer=False,
                ),
            ),
        )
    ],
    "syn_lifecycle_05": [
        _unit(
            "alpha_meeting_cancelled",
            "Alpha meeting on 2027-08-14 was cancelled.",
            _member(
                "cancelled",
                "supported",
                _candidate(
                    "August 14, 2027",
                    "meeting",
                    lifecycle_surface="cancelled",
                    lifecycle_role="cancelled",
                    relation="unspecified",
                    kind="unspecified",
                    lifecycle="cancelled",
                    normalized_value="2027-08-14",
                    requires_defer=False,
                ),
            ),
        ),
        _unit(
            "beta_meeting_schedule",
            "Beta meeting is scheduled for 2027-08-15.",
            _member(
                "schedule",
                "supported",
                _scheduled(
                    "August 15, 2027",
                    "meeting",
                    normalized_value="2027-08-15",
                    requires_defer=False,
                ),
                _occurrence_without_lifecycle(
                    "August 15, 2027",
                    "meeting",
                    kind="planned",
                    normalized_value="2027-08-15",
                    requires_defer=False,
                ),
            ),
        ),
    ],
    "syn_lifecycle_06": [
        _unit(
            "review_meeting_occurrence",
            "The review meeting occurred on 2027-08-09.",
            _member(
                "occurrence",
                "supported",
                _occurrence_without_lifecycle(
                    "August 9, 2027",
                    "meeting",
                    kind="actual",
                    normalized_value="2027-08-09",
                    requires_defer=False,
                    quality="exact",
                ),
                _occurrence_without_lifecycle(
                    "August 9, 2027",
                    "review",
                    kind="actual",
                    normalized_value="2027-08-09",
                    requires_defer=True,
                    quality="exact",
                ),
            ),
        ),
        _unit(
            "review_meeting_completion",
            "The review meeting completed that afternoon.",
            _member(
                "completion",
                "supported",
                _candidate(
                    "that afternoon",
                    "meeting",
                    expression_form="coarse_relative",
                    lifecycle_surface="completed",
                    lifecycle_role="completed",
                    relation="unspecified",
                    kind="unspecified",
                    lifecycle="completed",
                    normalized_value=None,
                    requires_defer=True,
                ),
                *[
                    _candidate(
                        "August 9, 2027",
                        subject,
                        lifecycle_surface="completed",
                        lifecycle_role="completed",
                        relation=relation,
                        kind=kind,
                        lifecycle=lifecycle,
                        normalized_value="2027-08-09",
                        requires_defer=requires_defer,
                        quality="partial",
                    )
                    for subject in ("meeting", "review")
                    for relation, kind, lifecycle in (
                        ("unspecified", "unspecified", "completed"),
                        ("unspecified", "unspecified", "unknown"),
                    )
                    for requires_defer in (
                        (False, True) if lifecycle == "completed" else (True,)
                    )
                ],
            ),
        ),
    ],
    "syn_lifecycle_07": [
        _unit(
            "meeting_cancellation_occurrence",
            "The meeting was cancelled on 2027-08-14.",
            _member(
                "cancelled",
                "supported",
                _candidate(
                    "August 14, 2027",
                    "meeting",
                    lifecycle_surface="cancelled",
                    lifecycle_role="cancelled",
                    relation="unspecified",
                    kind="unspecified",
                    lifecycle="cancelled",
                    normalized_value="2027-08-14",
                    requires_defer=False,
                ),
            ),
        ),
        _unit(
            "meeting_scheduled_occurrence",
            "The meeting was scheduled for 2027-08-10.",
            _member(
                "scheduled",
                "supported",
                _scheduled(
                    "August 10, 2027",
                    "meeting",
                    normalized_value="2027-08-10",
                    requires_defer=False,
                ),
            ),
        ),
    ],
    "syn_ambiguous_01": [
        _unit(
            "strategy_meeting_numeric_date",
            "The strategy meeting is scheduled for the ambiguous date 7/8/2027.",
            _member(
                "schedule",
                "supported",
                _scheduled(
                    "7/8/2027",
                    "meeting",
                    expression_form="numeric_date",
                    normalized_value=None,
                    requires_defer=True,
                ),
                _occurrence_without_lifecycle(
                    "7/8/2027",
                    "meeting",
                    expression_form="numeric_date",
                    kind="planned",
                    normalized_value=None,
                    requires_defer=True,
                ),
            ),
        )
    ],
    "syn_ambiguous_02": [
        _unit(
            "investor_call_schedule",
            "The investor call is scheduled for 2027-08-14 16:30 PDT.",
            _member(
                "schedule",
                "supported",
                _scheduled(
                    "August 14, 2027 at 4:30 PM PDT",
                    "call",
                    expression_form="date_time",
                    normalized_value="2027-08-14T16:30:00-07:00",
                    requires_defer=True,
                ),
                _occurrence_without_lifecycle(
                    "August 14, 2027 at 4:30 PM PDT",
                    "call",
                    expression_form="date_time",
                    kind="planned",
                    normalized_value="2027-08-14T16:30:00-07:00",
                    requires_defer=True,
                ),
            ),
        )
    ],
    "syn_ambiguous_03": [
        _unit(
            "design_review_friday",
            "The design review is Friday.",
            _member(
                "occurrence",
                "supported",
                _occurrence_without_lifecycle(
                    "Friday",
                    "review",
                    expression_form="relative_date",
                    kind="unspecified",
                    normalized_value=None,
                    requires_defer=True,
                    quality="exact",
                ),
            ),
        )
    ],
    "syn_ambiguous_04": [
        _unit(
            "juniper_interview_date_options",
            "Juniper Interview has two possible dates: 2027-08-18 or 2027-08-19.",
            *[
                _member(
                    f"option_{day}",
                    "uncertain",
                    _occurrence_without_lifecycle(
                        f"August {day}, 2027",
                        "Juniper Interview",
                        expression_field="body",
                        subject_type="event_title_candidate",
                        subject_field="subject",
                        kind="unspecified",
                        normalized_value=f"2027-08-{day}",
                        requires_defer=True,
                        quality="partial",
                    ),
                    _occurrence_without_lifecycle(
                        f"August {day}, 2027",
                        "Interview",
                        expression_field="body",
                        subject_field="subject",
                        kind="unspecified",
                        normalized_value=f"2027-08-{day}",
                        requires_defer=True,
                        quality="partial",
                    ),
                    baseline_frontier_grade="partial",
                )
                for day in (18, 19)
            ],
            baseline_frontier_grade="partial",
        )
    ],
    "syn_ambiguous_05": [
        _unit(
            "product_summit_range",
            "The product summit runs 2027-08-14 through 2027-08-16.",
            _member(
                "occurrence",
                "supported",
                _occurrence_without_lifecycle(
                    "August 14-16, 2027",
                    "summit",
                    expression_form="date_range",
                    kind="unspecified",
                    normalized_value="2027-08-14/2027-08-17",
                    requires_defer=True,
                    quality="exact",
                ),
            ),
        )
    ],
    "syn_ambiguous_06": [
        _unit(
            "northwind_flight_arrival",
            "The Northwind flight arrives 2027-08-14 at 18:10 local time.",
            _member(
                "arrival_boundary",
                "supported",
                _candidate(
                    "August 14, 2027 at 6:10 PM.",
                    "arrives",
                    expression_form="date_time",
                    subject_type="boundary",
                    relation="unspecified",
                    kind="unspecified",
                    lifecycle="none",
                    normalized_value=None,
                    requires_defer=True,
                ),
            ),
        )
    ],
    "syn_ambiguous_07": [
        _unit(
            "report_deadline_before_lunch",
            "The revised report must be sent before lunch tomorrow.",
            _member(
                "deadline",
                "supported",
                _candidate(
                    "lunch tomorrow",
                    "send",
                    expression_form="coarse_relative",
                    subject_type="action",
                    relation="deadline",
                    kind="planned",
                    lifecycle="none",
                    normalized_value="2027-08-11",
                    requires_defer=True,
                ),
            ),
        )
    ],
    "syn_ambiguous_08": [
        _unit(
            "research_sync_recurrence",
            "The research sync recurs every Tuesday.",
            _member(
                "recurrence",
                "supported",
                _occurrence_without_lifecycle(
                    "every Tuesday",
                    "sync",
                    expression_form="recurrence",
                    subject_type="event_predicate",
                    kind="unspecified",
                    normalized_value=None,
                    requires_defer=True,
                    quality="exact",
                ),
            ),
        )
    ],
    "syn_hard_01": [
        _unit(
            "benefits_policy_effective",
            "The new benefits policy takes effect on 2027-08-01.",
            _member(
                "effective_occurrence",
                "supported",
                _candidate(
                    "August 1, 2027",
                    "becomes effective",
                    subject_type="event_predicate",
                    relation="occurrence",
                    kind="unspecified",
                    lifecycle="none",
                    normalized_value="2027-08-01",
                    requires_defer=True,
                ),
            ),
        )
    ],
    "syn_hard_02": [
        _unit(
            "registration_opens",
            "Registration opens on 2027-08-12.",
            _member(
                "opening",
                "supported",
                _candidate(
                    "August 12, 2027",
                    "Registration",
                    subject_type="event",
                    relation="occurrence",
                    kind="planned",
                    lifecycle="none",
                    normalized_value="2027-08-12",
                    requires_defer=False,
                ),
            ),
        ),
        _unit(
            "registration_closes",
            "Registration closes on 2027-08-20.",
            _member(
                "deadline",
                "supported",
                _candidate(
                    "August 20, 2027",
                    "closes",
                    subject_type="event_predicate",
                    relation="deadline",
                    kind="planned",
                    lifecycle="none",
                    normalized_value="2027-08-20",
                    requires_defer=True,
                ),
            ),
        ),
    ],
    "syn_dense_01": [
        _unit(
            "dense_alpha_interview",
            "Alpha interview is scheduled for 2027-08-14 at 09:00 local time.",
            _member(
                "schedule",
                "supported",
                _scheduled(
                    "August 14, 2027 at 9:00 AM",
                    "interview",
                    expression_form="date_time",
                    expression_field="body",
                    normalized_value=None,
                    requires_defer=True,
                ),
                _occurrence_without_lifecycle(
                    "August 14, 2027 at 9:00 AM",
                    "interview",
                    expression_form="date_time",
                    expression_field="body",
                    kind="planned",
                    normalized_value=None,
                    requires_defer=True,
                ),
                _candidate(
                    "August 14, 2027 at 9:00 AM",
                    "interview",
                    expression_form="date_time",
                    expression_field="body",
                    lifecycle_surface="scheduled",
                    lifecycle_role="scheduled",
                    lifecycle_field="body",
                    relation="unspecified",
                    kind="unspecified",
                    lifecycle="unknown",
                    normalized_value=None,
                    requires_defer=True,
                    quality="partial",
                ),
            ),
        ),
        _unit(
            "dense_beta_workshop",
            "Beta workshop is scheduled for 2027-08-16 at 14:00 local time.",
            _member(
                "schedule",
                "supported",
                _scheduled(
                    "August 16, 2027 at 2:00 PM",
                    "workshop",
                    expression_form="date_time",
                    expression_field="body",
                    normalized_value=None,
                    requires_defer=True,
                ),
                _occurrence_without_lifecycle(
                    "August 16, 2027 at 2:00 PM",
                    "workshop",
                    expression_form="date_time",
                    expression_field="body",
                    kind="planned",
                    normalized_value=None,
                    requires_defer=True,
                ),
                _candidate(
                    "August 16, 2027 at 2:00 PM",
                    "workshop",
                    expression_form="date_time",
                    expression_field="body",
                    lifecycle_surface="scheduled",
                    lifecycle_role="scheduled",
                    lifecycle_field="body",
                    relation="unspecified",
                    kind="unspecified",
                    lifecycle="unknown",
                    normalized_value=None,
                    requires_defer=True,
                    quality="partial",
                ),
            ),
        ),
        _unit(
            "dense_board_packet_deadline",
            "The board packet must be submitted by 2027-08-18.",
            _member(
                "deadline",
                "supported",
                _candidate(
                    "August 18, 2027",
                    "submit",
                    expression_field="body",
                    subject_type="action",
                    relation="deadline",
                    kind="planned",
                    lifecycle="none",
                    normalized_value="2027-08-18",
                    requires_defer=False,
                ),
            ),
        ),
    ],
    "syn_mixed_01": [
        _unit(
            "atlas_interview_current_schedule",
            "The authored update schedules Atlas Interview for 2027-08-21 10:00 PDT.",
            _member(
                "schedule",
                "supported",
                _scheduled(
                    "August 21, 2027 at 10:00 AM PDT",
                    "interview",
                    expression_form="date_time",
                    expression_field="body",
                    subject_field="body",
                    normalized_value="2027-08-21T10:00:00-07:00",
                    requires_defer=True,
                ),
                _occurrence_without_lifecycle(
                    "August 21, 2027 at 10:00 AM PDT",
                    "interview",
                    expression_form="date_time",
                    expression_field="body",
                    subject_field="body",
                    kind="planned",
                    normalized_value="2027-08-21T10:00:00-07:00",
                    requires_defer=True,
                ),
                _occurrence_without_lifecycle(
                    "August 21, 2027 at 10:00 AM PDT",
                    "Atlas Interview Update",
                    expression_form="date_time",
                    expression_field="body",
                    subject_type="event_title_candidate",
                    subject_field="subject",
                    kind="planned",
                    normalized_value="2027-08-21T10:00:00-07:00",
                    requires_defer=True,
                ),
                _occurrence_without_lifecycle(
                    "August 21, 2027 at 10:00 AM PDT",
                    "Interview",
                    expression_form="date_time",
                    expression_field="body",
                    subject_field="subject",
                    kind="planned",
                    normalized_value="2027-08-21T10:00:00-07:00",
                    requires_defer=True,
                ),
                _candidate(
                    "August 21, 2027 at 10:00 AM PDT",
                    "Atlas Interview Update",
                    expression_form="date_time",
                    expression_field="body",
                    subject_type="event_title_candidate",
                    subject_field="subject",
                    lifecycle_surface="scheduled",
                    lifecycle_role="scheduled",
                    lifecycle_field="body",
                    relation="unspecified",
                    kind="unspecified",
                    lifecycle="unknown",
                    normalized_value="2027-08-21T10:00:00-07:00",
                    requires_defer=True,
                    quality="partial",
                ),
                _candidate(
                    "August 21, 2027 at 10:00 AM PDT",
                    "Interview",
                    expression_form="date_time",
                    expression_field="body",
                    subject_field="subject",
                    lifecycle_surface="scheduled",
                    lifecycle_role="scheduled",
                    lifecycle_field="body",
                    relation="unspecified",
                    kind="unspecified",
                    lifecycle="unknown",
                    normalized_value="2027-08-21T10:00:00-07:00",
                    requires_defer=True,
                    quality="partial",
                ),
            ),
        )
    ],
}


def semantic_gold_for(sample_id: str) -> list[dict[str, Any]]:
    """Return semantic-locator gold; unmatched candidates are default-negative."""

    return SEMANTIC_GOLD.get(sample_id, [])
