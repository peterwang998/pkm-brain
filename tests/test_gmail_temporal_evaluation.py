from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from pkm_brain.gmail_temporal_evaluation import (
    HistoricalGmailGateConfig,
    evaluate_historical_gmail_projection,
)


SECRET = b"private-test-key-for-stable-samples"


@dataclass(frozen=True)
class Candidate:
    relation: str
    kind: str
    precision: str
    resolution_basis: tuple[str, ...]


def detector(*, text: str, message_internal_at: str | None, chunk_id: str):
    del message_internal_at, chunk_id
    if "TEMPORAL_MARKER" not in text:
        return ()
    return (
        Candidate(
            relation="deadline" if "DEADLINE_MARKER" in text else "occurrence",
            kind="planned",
            precision="day",
            resolution_basis=("explicit_month_day_year",),
        ),
    )


def projection(
    *,
    thread_id: str,
    revision: str,
    updated_at: str,
    body: str,
    eligible: bool,
    delivery: str = "human",
    importance: str = "durable_candidate",
    actionability: str = "informational",
    deleted: bool = False,
    projection_version: int = 6,
    classifier_version: int = 5,
    captured_at: str = "2026-07-01T16:00:00+00:00",
    subject: str | None = None,
    additional_messages: tuple[str, ...] = (),
) -> str:
    internal_at = "2026-07-01T16:00:00+00:00"
    message_ids = [
        f"provider-message-{thread_id}{f'-{index}' if index > 1 else ''}"
        for index in range(1, len(additional_messages) + 2)
    ]
    rendered_messages: list[str] = []
    for index, (message_id, payload) in enumerate(
        zip(message_ids, (body, *additional_messages)), start=1
    ):
        subject_line = (
            f"\nSubject: {subject}" if index == 1 and subject is not None else ""
        )
        rendered_messages.append(
            f"## Message {index} — {internal_at} — {message_id}\n\n"
            "From: private-sender@example.test\n"
            f"To: owner-private@example.test{subject_line}\n\n"
            f"{payload}"
        )
    rendered_body = "# Email thread: Private synthetic title\n\n" + "\n\n".join(
        rendered_messages
    )
    timestamp_rows: list[str] = []
    search_start = 0
    for index, (message_id, rendered_message) in enumerate(
        zip(message_ids, rendered_messages), start=1
    ):
        message_start = rendered_body.index(rendered_message, search_start)
        message_end = message_start + len(rendered_message)
        search_start = message_end
        timestamp_rows.append(
            f'''  - message_id: "{message_id}"
    internal_date: "{internal_at}"
    start_offset: {message_start}
    end_offset: {message_end}'''
        )
    message_id_yaml = ", ".join(f'"{value}"' for value in message_ids)
    timestamp_yaml = "\n".join(timestamp_rows)
    return f'''---
title: "Private title for {thread_id}"
source_type: gmail_thread
gmail_account_key: "owner-private@example.test"
gmail_thread_id: "{thread_id}"
gmail_source_revision: "{revision}"
gmail_projection_version: {projection_version}
gmail_classifier_version: {classifier_version}
archive_updated_at: "{updated_at}"
captured_at: "{captured_at}"
created_at: "2026-07-01T16:00:00+00:00"
gmail_message_ids: [{message_id_yaml}]
gmail_message_timestamps_version: 1
retained_message_count: {len(message_ids)}
delivery_kind: {delivery}
fact_importance: {importance}
actionability: {actionability}
fact_eligible: {str(eligible).lower()}
deleted: {str(deleted).lower()}
gmail_message_timestamps:
{timestamp_yaml}
---

{rendered_body}'''


def corpus(tmp_path: Path) -> Path:
    root = tmp_path / "gmail"
    root.mkdir()
    (root / "b.md").write_text(
        projection(
            thread_id="private-thread-a",
            revision="a" * 64,
            updated_at="2026-07-01T16:00:00+00:00",
            body="A routine historical update containing SECRET_ALPHA.",
            eligible=False,
        ),
        encoding="utf-8",
    )
    (root / "a.md").write_text(
        projection(
            thread_id="private-thread-a",
            revision="b" * 64,
            updated_at="2026-07-02T16:00:00+00:00",
            body=(
                "TEMPORAL_MARKER The planning meeting was rescheduled for "
                "July 22, 2026. SECRET_BRAVO"
            ),
            eligible=True,
            importance="important_temporal",
            actionability="time_sensitive",
        ),
        encoding="utf-8",
    )
    (root / "c.md").write_text(
        projection(
            thread_id="private-thread-ad",
            revision="c" * 64,
            updated_at="2026-07-03T16:00:00+00:00",
            body=("Sale event on July 23, 2026. BEGIN:VCALENDAR SECRET_CHARLIE"),
            eligible=False,
            delivery="bulk",
            importance="advertising",
        ),
        encoding="utf-8",
    )
    return root


def evaluate(root: Path, **changes):
    kwargs = {
        "sample_secret": SECRET,
        "detector": detector,
        "expected_file_count": 3,
        "baseline_fact_eligible_rate": 0.5,
    }
    kwargs.update(changes)
    return evaluate_historical_gmail_projection(root, **kwargs)


def test_report_is_aggregate_only_private_and_deterministic(tmp_path: Path) -> None:
    root = corpus(tmp_path)

    first = evaluate(root)
    second = evaluate(root)

    assert first == second
    assert first["privacy"]["assertion_passed"] is True
    serialized = json.dumps(first, sort_keys=True)
    for private_value in (
        "SECRET_ALPHA",
        "SECRET_BRAVO",
        "SECRET_CHARLIE",
        "private-thread-a",
        "private-thread-ad",
        "provider-message",
        "owner-private@example.test",
        "Private title",
    ):
        assert private_value not in serialized
    sample_ids = first["historical_unique_revisions"]["sample_ids"]
    assert all(
        value.startswith("gte_") and len(value) == 28
        for values in sample_ids.values()
        for value in values
    )


def test_latest_and_historical_funnels_and_transitions_are_separate(
    tmp_path: Path,
) -> None:
    report = evaluate(corpus(tmp_path))

    assert report["coverage"] == {
        "files_discovered": 3,
        "files_processed": 3,
        "expected_files": 3,
        "gmail_projection_files": 3,
        "non_gmail_files": 0,
        "invalid_files": 0,
        "unique_revisions": 3,
        "duplicate_projection_files": 0,
        "collapsed_projection_variants": 0,
        "opaque_thread_lineages": 2,
        "current_active_threads": 2,
        "current_deleted_threads": 0,
        "source_coverage_rate": 1.0,
        "detector_coverage_rate": 1.0,
        "historical_coverage_rate": 1.0,
    }
    current = report["current_latest_per_thread"]["funnel"]
    assert current["total"] == 2
    assert current["eligible"] == 1
    assert current["temporal_evidence_bearing"] == 2
    assert current["parsed"] == 1
    assert current["normalized"] == 1
    assert current["event"] == 1
    assert current["structured_ics_available"] == 1
    history = report["historical_unique_revisions"]["funnel"]
    assert history["total"] == 3
    transitions = report["historical_transitions"]
    assert transitions["revision_transitions"] == 1
    assert transitions["eligibility_gained"] == 1
    assert transitions["temporal_evidence_gained"] == 1
    assert transitions["parsed_gained"] == 1
    assert transitions["lifecycle_cues_changed"] == 1
    strata = report["current_latest_per_thread"]["classifier_noise_strata"]
    assert any(
        item["delivery_kind"] == "bulk"
        and item["fact_importance"] == "advertising"
        and item["fact_eligible"] is False
        for item in strata
    )


def test_pragmatic_gates_distinguish_proxies_from_labels(tmp_path: Path) -> None:
    root = corpus(tmp_path)
    unlabeled = evaluate(root)

    checks = {item["name"]: item for item in unlabeled["gates"]["checks"]}
    assert checks["explicit_date_recall_proxy"]["status"] == "pass"
    assert checks["explicit_date_recall_proxy"]["basis"] == (
        "historical_proxy_not_gold"
    )
    important_proxy = unlabeled["quality_metrics"][
        "current_important_temporal_detection"
    ]
    assert important_proxy == {
        "value": 1.0,
        "numerator": 1,
        "denominator": 1,
        "basis": "historical_classifier_proxy_not_gold",
    }
    assert checks["current_important_temporal_detection_proxy"]["status"] == "pass"
    assert checks["calibration_sample_size"]["status"] == "not_evaluated"
    assert checks["calibration_annotation_coverage"]["status"] == "not_evaluated"
    assert checks["supported_time_precision"]["status"] == "not_evaluated"
    assert checks["final_judge_acceptance"]["status"] == "not_evaluated"
    assert checks["critical_errors"]["status"] == "not_evaluated"
    assert checks["cross_occurrence_errors"]["status"] == "not_evaluated"
    assert unlabeled["gates"]["promotion_ready"] is False

    direct_id = unlabeled["historical_unique_revisions"]["sample_ids"][
        "calibration_direct_hit"
    ][0]
    sparse = evaluate(
        root,
        annotations={
            direct_id: {
                "temporal_relevant": True,
                "supported": True,
                "final_judge_acceptable": True,
                "critical_error": False,
                "cross_occurrence_error": False,
            }
        },
        calibration_cohort=[direct_id],
    )
    sparse_checks = {item["name"]: item for item in sparse["gates"]["checks"]}
    assert sparse_checks["calibration_sample_size"]["status"] == "fail"
    assert sparse_checks["calibration_minimum_stratum_count"]["status"] == "fail"
    assert len(sparse["calibration_cohort"]["manifest_sha256"]) == 64
    assert sparse["gates"]["promotion_ready"] is False

    negative_id = unlabeled["historical_unique_revisions"]["sample_ids"][
        "calibration_bulk_advertising_negative"
    ][0]
    relaxed_test_gates = HistoricalGmailGateConfig(
        minimum_labeled_sample_size=2,
        minimum_labeled_per_required_stratum=1,
        required_calibration_strata=(
            "direct_hit",
            "human_mail_lead",
            "lifecycle_language",
            "bulk_advertising_negative",
        ),
    )
    selectively_labeled = evaluate(
        root,
        gates=relaxed_test_gates,
        calibration_cohort=[direct_id, negative_id],
        annotations={
            direct_id: {
                "temporal_relevant": True,
                "supported": True,
                "final_judge_acceptable": True,
                "critical_error": False,
                "cross_occurrence_error": False,
            }
        },
    )
    selective_checks = {
        item["name"]: item for item in selectively_labeled["gates"]["checks"]
    }
    assert selective_checks["calibration_annotation_coverage"]["status"] == "fail"
    assert selective_checks["supported_time_precision"]["status"] == ("not_evaluated")
    assert selectively_labeled["gates"]["promotion_ready"] is False

    labeled = evaluate(
        root,
        gates=relaxed_test_gates,
        calibration_cohort=[direct_id, negative_id],
        annotations={
            direct_id: {
                "temporal_relevant": True,
                "supported": True,
                "final_judge_acceptable": True,
                "critical_error": False,
                "cross_occurrence_error": False,
            },
            negative_id: {
                "temporal_relevant": False,
                "critical_error": False,
                "cross_occurrence_error": False,
            },
        },
    )
    labeled_checks = {item["name"]: item for item in labeled["gates"]["checks"]}
    assert labeled_checks["calibration_sample_size"]["status"] == "pass"
    assert labeled_checks["calibration_annotation_coverage"]["status"] == "pass"
    assert labeled_checks["calibration_minimum_stratum_count"]["status"] == "pass"
    assert labeled_checks["human_temporal_recall"]["status"] == "pass"
    assert labeled_checks["supported_time_precision"]["status"] == "pass"
    assert labeled_checks["final_judge_acceptance"]["status"] == "pass"
    assert (
        labeled["calibration_cohort"]["manifest_sha256"]
        == (selectively_labeled["calibration_cohort"]["manifest_sha256"])
    )
    assert labeled["gates"]["promotion_ready"] is True


def test_nondeterministic_detector_is_a_zero_tolerance_failure(tmp_path: Path) -> None:
    calls = 0

    def unstable(*, text: str, message_internal_at: str | None, chunk_id: str):
        nonlocal calls
        del message_internal_at, chunk_id
        calls += 1
        if "TEMPORAL_MARKER" not in text or calls % 2 == 0:
            return ()
        return detector(text=text, message_internal_at=None, chunk_id="opaque")

    report = evaluate(corpus(tmp_path), detector=unstable)
    checks = {item["name"]: item for item in report["gates"]["checks"]}
    assert report["quality_metrics"]["nondeterministic_revisions"]["value"] >= 1
    assert checks["nondeterministic_revisions"]["status"] == "fail"


def test_gate_thresholds_are_configurable(tmp_path: Path) -> None:
    report = evaluate(
        corpus(tmp_path),
        gates=HistoricalGmailGateConfig(
            minimum_explicit_date_recall_proxy=1.0,
            maximum_fact_eligible_rate_delta=0.0,
        ),
    )
    checks = {item["name"]: item for item in report["gates"]["checks"]}
    assert checks["explicit_date_recall_proxy"]["threshold"] == 1.0
    assert checks["fact_eligible_rate_delta"]["threshold"] == 0.0


def test_default_evidence_first_detector_integrates_without_content_output(
    tmp_path: Path,
) -> None:
    report = evaluate_historical_gmail_projection(
        corpus(tmp_path),
        sample_secret=SECRET,
        expected_file_count=3,
        baseline_fact_eligible_rate=0.5,
    )

    dimensions = report["historical_unique_revisions"]["candidate_dimensions"]
    assert dimensions["relation"]["occurrence"] == 1
    assert dimensions["resolution_basis"]["explicit_month_day_year"] == 1


def test_connector_internal_date_heading_is_not_source_date_evidence(
    tmp_path: Path,
) -> None:
    root = tmp_path / "gmail"
    root.mkdir()
    (root / "only.md").write_text(
        projection(
            thread_id="heading-date-negative-control",
            revision="d" * 64,
            updated_at="2026-07-04T16:00:00+00:00",
            body="The meeting agenda is attached, with no scheduled date yet.",
            eligible=True,
            importance="important_temporal",
        ),
        encoding="utf-8",
    )

    report = evaluate_historical_gmail_projection(
        root,
        sample_secret=SECRET,
        detector=detector,
        expected_file_count=1,
    )

    funnel = report["current_latest_per_thread"]["funnel"]
    assert funnel["temporal_evidence_bearing"] == 0
    assert funnel["parsed"] == 0
    metric = report["quality_metrics"]["explicit_date_recall"]
    assert metric["denominator"] == 0
    assert metric["value"] is None


def test_explicit_date_proxy_requires_cue_and_date_in_same_message(
    tmp_path: Path,
) -> None:
    root = tmp_path / "gmail"
    root.mkdir()
    (root / "cross-message.md").write_text(
        projection(
            thread_id="cross-message-negative-control",
            revision="9" * 64,
            updated_at="2026-07-04T18:00:00+00:00",
            body="The meeting agenda is attached.",
            additional_messages=("July 26, 2026 works for the separate item.",),
            eligible=True,
            importance="important_temporal",
        ),
        encoding="utf-8",
    )

    report = evaluate_historical_gmail_projection(
        root,
        sample_secret=SECRET,
        detector=lambda **_kwargs: (),
        expected_file_count=1,
    )

    funnel = report["current_latest_per_thread"]["funnel"]
    assert funnel["temporal_evidence_bearing"] == 0
    metric = report["quality_metrics"]["explicit_date_recall"]
    assert metric["denominator"] == 0
    assert metric["value"] is None
    diagnostics = report["current_latest_per_thread"][
        "important_temporal_miss_diagnostics"
    ]
    assert diagnostics["cue_association"] == {
        "cue_present_unassociated": 0,
        "temporal_form_without_cue": 1,
        "no_temporal_form": 0,
    }


def test_untrusted_message_timestamp_index_fails_closed_without_fallback(
    tmp_path: Path,
) -> None:
    root = corpus(tmp_path)
    latest = root / "a.md"
    text = latest.read_text(encoding="utf-8")
    latest.write_text(
        text.replace(
            "gmail_message_timestamps_version: 1",
            "gmail_message_timestamps_version: 2",
        ),
        encoding="utf-8",
    )
    seen: list[str] = []

    def recording_detector(
        *, text: str, message_internal_at: str | None, chunk_id: str
    ):
        del message_internal_at, chunk_id
        seen.append(text)
        return detector(text=text, message_internal_at=None, chunk_id="opaque")

    report = evaluate(root, detector=recording_detector)

    assert not any("TEMPORAL_MARKER" in value for value in seen)
    assert report["coverage"]["detector_coverage_rate"] == 2 / 3
    current = report["current_latest_per_thread"]["funnel"]
    assert current["trusted_message_ranges_available"] == 1
    assert current["held"] == 1
    assert (
        len(
            report["current_latest_per_thread"]["sample_ids"][
                "trusted_message_range_unavailable"
            ]
        )
        == 1
    )


def test_determinism_compares_private_bounds_and_exact_spans(tmp_path: Path) -> None:
    calls = 0

    def span_unstable(*, text: str, message_internal_at: str | None, chunk_id: str):
        nonlocal calls
        del message_internal_at
        if "TEMPORAL_MARKER" not in text:
            return ()
        calls += 1
        span_end = 20 if calls % 2 else 21
        return (
            {
                "relation": "occurrence",
                "kind": "planned",
                "start_at": "2026-07-22",
                "end_at": None,
                "precision": "day",
                "resolution_basis": ("explicit_month_day_year",),
                "expression_span": {
                    "start": 10,
                    "end": span_end,
                    "chunk_id": chunk_id,
                },
                "cue_span": {"start": 0, "end": 9, "chunk_id": chunk_id},
            },
        )

    report = evaluate(corpus(tmp_path), detector=span_unstable)

    assert report["quality_metrics"]["nondeterministic_revisions"]["value"] == 1
    check = next(
        item
        for item in report["gates"]["checks"]
        if item["name"] == "nondeterministic_revisions"
    )
    assert check["status"] == "fail"


def test_renderer_variants_collapse_without_fake_mailbox_transition(
    tmp_path: Path,
) -> None:
    root = tmp_path / "gmail"
    root.mkdir()
    shared = {
        "thread_id": "same-private-thread",
        "revision": "e" * 64,
        "updated_at": "2026-07-05T16:00:00+00:00",
        "body": "TEMPORAL_MARKER The meeting is scheduled for July 25, 2026.",
    }
    (root / "old.md").write_text(
        projection(
            **shared,
            eligible=False,
            projection_version=5,
            classifier_version=4,
        ),
        encoding="utf-8",
    )
    (root / "new.md").write_text(
        projection(
            **shared,
            eligible=True,
            importance="important_temporal",
            projection_version=6,
            classifier_version=5,
        ),
        encoding="utf-8",
    )

    report = evaluate_historical_gmail_projection(
        root,
        sample_secret=SECRET,
        detector=detector,
        expected_file_count=2,
    )

    assert report["coverage"]["gmail_projection_files"] == 2
    assert report["coverage"]["unique_revisions"] == 1
    assert report["coverage"]["collapsed_projection_variants"] == 1
    assert report["current_latest_per_thread"]["funnel"]["eligible"] == 1
    assert report["historical_transitions"]["revision_transitions"] == 0


def test_invalid_projection_reduces_full_history_coverage(tmp_path: Path) -> None:
    root = corpus(tmp_path)
    (root / "broken.md").write_text(
        "---\nsource_type: gmail_thread\n---\n", encoding="utf-8"
    )

    report = evaluate(
        root,
        expected_file_count=4,
    )

    assert report["coverage"]["invalid_files"] == 1
    assert report["coverage"]["source_coverage_rate"] == 0.75
    coverage_gate = next(
        item
        for item in report["gates"]["checks"]
        if item["name"] == "historical_coverage"
    )
    assert coverage_gate["status"] == "fail"


def test_important_temporal_misses_are_content_safely_stratified(
    tmp_path: Path,
) -> None:
    root = tmp_path / "gmail"
    root.mkdir()
    cases = (
        (
            "full-year-subject",
            "The details are included.",
            "Meeting scheduled for July 22, 2026",
            False,
        ),
        (
            "inferred-body",
            "The meeting is scheduled for July 23.",
            None,
            False,
        ),
        (
            "numeric-body-ics",
            "The meeting is scheduled for 7/24/2026. BEGIN:VCALENDAR",
            None,
            True,
        ),
        (
            "relative-both",
            "The meeting is scheduled tomorrow.",
            "Meeting tomorrow",
            False,
        ),
        (
            "time-only-body",
            "The meeting is scheduled at 3:30 pm.",
            None,
            False,
        ),
        (
            "no-cue-body",
            "Please keep July 25, 2026 in mind.",
            None,
            False,
        ),
    )
    for index, (thread_id, body, subject, _ics) in enumerate(cases):
        (root / f"case-{index}.md").write_text(
            projection(
                thread_id=thread_id,
                revision=f"{index + 1:064x}",
                updated_at=f"2026-07-{index + 1:02d}T16:00:00+00:00",
                body=body,
                subject=subject,
                eligible=True,
                importance="important_temporal",
            ),
            encoding="utf-8",
        )

    report = evaluate_historical_gmail_projection(
        root,
        sample_secret=SECRET,
        detector=lambda **_kwargs: (),
        expected_file_count=len(cases),
    )

    diagnostics = report["current_latest_per_thread"][
        "important_temporal_miss_diagnostics"
    ]
    assert diagnostics["misses"] == 6
    assert diagnostics["by_temporal_form"] == {
        "explicit_full_year": 2,
        "inferred_year_month_day": 1,
        "numeric": 1,
        "relative_or_weekday": 1,
        "time_only": 1,
    }
    assert diagnostics["cue_association"] == {
        "cue_present_unassociated": 5,
        "temporal_form_without_cue": 1,
        "no_temporal_form": 0,
    }
    assert diagnostics["structured_ics_available"] == 1
    assert diagnostics["temporal_form_location"] == {
        "subject_only": 1,
        "body_only": 4,
        "subject_and_body": 1,
        "none": 0,
    }
    serialized = json.dumps(diagnostics, sort_keys=True)
    for private_value in ("July", "meeting", "BEGIN:VCALENDAR", "3:30"):
        assert private_value.casefold() not in serialized.casefold()


def test_deadline_funnel_is_candidate_backed_and_proxy_is_explicit(
    tmp_path: Path,
) -> None:
    root = tmp_path / "gmail"
    root.mkdir()
    (root / "deadline.md").write_text(
        projection(
            thread_id="deadline-private-thread",
            revision="f" * 64,
            updated_at="2026-07-09T16:00:00+00:00",
            body="The submission deadline is July 30, 2026.",
            eligible=True,
            importance="important_temporal",
        ),
        encoding="utf-8",
    )

    report = evaluate_historical_gmail_projection(
        root,
        sample_secret=SECRET,
        detector=lambda **_kwargs: (),
    )

    funnel = report["current_latest_per_thread"]["funnel"]
    assert funnel["deadline"] == 0
    assert funnel["deadline_evidence_proxy"] == 1
    assert report["current_latest_per_thread"]["candidate_dimensions"]["relation"] == {}


def test_detector_yield_is_stratified_by_noise_class(tmp_path: Path) -> None:
    root = tmp_path / "gmail"
    root.mkdir()
    cases = (
        ("ad", "bulk", "advertising", False),
        ("routine", "human", "routine", False),
        ("signal", "human", "durable_candidate", True),
        ("other", "human", "unknown", False),
    )
    for index, (thread_id, delivery, importance, eligible) in enumerate(cases):
        (root / f"noise-{index}.md").write_text(
            projection(
                thread_id=thread_id,
                revision=f"{index + 10:064x}",
                updated_at=f"2026-07-{index + 10:02d}T16:00:00+00:00",
                body="TEMPORAL_MARKER The meeting is scheduled for July 30, 2026.",
                eligible=eligible,
                delivery=delivery,
                importance=importance,
            ),
            encoding="utf-8",
        )

    report = evaluate_historical_gmail_projection(
        root,
        sample_secret=SECRET,
        detector=detector,
    )

    strata = report["current_latest_per_thread"]["detector_yield_by_noise_class"]
    for noise_class in (
        "advertising_or_bulk",
        "routine",
        "fact_eligible_signal",
        "other_suppressed",
    ):
        assert strata[noise_class] == {
            "revisions": 1,
            "parsed_revisions": 1,
            "parsed_revision_rate": 1.0,
            "normalized_revisions": 1,
            "candidates": 1,
        }


def test_evidence_digest_counts_true_revision_variation(tmp_path: Path) -> None:
    root = tmp_path / "gmail"
    root.mkdir()
    for index, body in enumerate(
        (
            "No temporal marker yet.",
            "No temporal marker yet.",
            "TEMPORAL_MARKER The meeting was rescheduled for July 31, 2026.",
            "TEMPORAL_MARKER The meeting was rescheduled for July 31, 2026.",
        )
    ):
        (root / f"revision-{index}.md").write_text(
            projection(
                thread_id="evidence-variation-thread",
                revision=f"{index + 20:064x}",
                updated_at=f"2026-07-{index + 20:02d}T16:00:00+00:00",
                body=body,
                eligible=index >= 2,
                importance="important_temporal" if index >= 2 else "routine",
            ),
            encoding="utf-8",
        )

    transitions = evaluate_historical_gmail_projection(
        root,
        sample_secret=SECRET,
        detector=detector,
    )["historical_transitions"]

    assert transitions["revision_transitions"] == 3
    assert transitions["threads_with_evidence_content_variation"] == 1
    assert transitions["evidence_content_unchanged"] == 2
    assert transitions["evidence_content_changed"] == 1
    assert transitions["parsed_changed_with_evidence_content"] == 1
    assert transitions["candidate_temporal_assignment_changed"] == 1
    assert (
        transitions["candidate_temporal_assignment_changed_with_evidence_content"] == 1
    )


def test_current_selection_mirrors_gmail_revision_rank(tmp_path: Path) -> None:
    root = tmp_path / "gmail"
    root.mkdir()
    shared = {
        "thread_id": "rank-private-thread",
        "updated_at": "2026-07-10T16:00:00+00:00",
        "body": "No event date in this synthetic revision.",
    }
    (root / "higher-source-revision.md").write_text(
        projection(
            **shared,
            revision="f" * 64,
            projection_version=6,
            captured_at="2026-07-12T16:00:00+00:00",
            eligible=True,
        ),
        encoding="utf-8",
    )
    (root / "higher-projection.md").write_text(
        projection(
            **shared,
            revision="0" * 64,
            projection_version=7,
            captured_at="2026-07-11T16:00:00+00:00",
            eligible=False,
        ),
        encoding="utf-8",
    )

    projection_winner = evaluate_historical_gmail_projection(
        root,
        sample_secret=SECRET,
        detector=detector,
    )
    assert projection_winner["current_latest_per_thread"]["funnel"]["eligible"] == 0

    (root / "higher-capture.md").write_text(
        projection(
            **shared,
            revision="1" * 64,
            projection_version=7,
            captured_at="2026-07-13T16:00:00+00:00",
            eligible=True,
        ),
        encoding="utf-8",
    )
    capture_winner = evaluate_historical_gmail_projection(
        root,
        sample_secret=SECRET,
        detector=detector,
    )
    assert capture_winner["current_latest_per_thread"]["funnel"]["eligible"] == 1
