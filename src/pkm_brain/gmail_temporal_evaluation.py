from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections import Counter, defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .chunking import strip_frontmatter
from .source_dates import gmail_message_source_evidence
from .wiki import parse_frontmatter


GMAIL_TEMPORAL_EVALUATION_SCHEMA_VERSION = 1

_DELIVERY_KINDS = {"human", "mixed", "transactional", "bulk", "unknown"}
_IMPORTANCE_KINDS = {
    "durable_candidate",
    "important_temporal",
    "routine",
    "advertising",
    "unknown",
}
_ACTIONABILITY_KINDS = {
    "action_required",
    "time_sensitive",
    "informational",
    "unknown",
}
_RELATIONS = {"occurrence", "deadline"}
_KINDS = {"planned", "actual"}
_PRECISIONS = {"day", "exact"}
_PROVIDER_MESSAGE_ID = re.compile(r"^[A-Za-z0-9_.-]+$")
_SAFE_BASES = {
    "clock_time_discarded_ambiguous_meridiem",
    "clock_time_discarded_invalid_or_ambiguous_timezone",
    "clock_time_discarded_missing_timezone",
    "day_month_year_inferred_from_message_internal_at",
    "explicit_clock_time",
    "explicit_day_month_year",
    "explicit_iana_timezone",
    "explicit_iso_date",
    "explicit_month_day_year",
    "explicit_numeric_utc_offset",
    "explicit_timezone_abbreviation",
    "explicit_utc_designator",
    "fixed_north_american_timezone_abbreviation",
    "exclusive_until_date_range",
    "inclusive_textual_date_range",
    "month_day_year_inferred_from_message_internal_at",
    "range_year_cohered_to_explicit_endpoint",
    "relative_this_coming_weekday_from_message_internal_at",
    "relative_today_from_message_internal_at",
    "relative_tomorrow_from_message_internal_at",
    "textual_range_with_explicit_year",
    "textual_range_year_inferred_from_message_internal_at",
}

_EXPLICIT_DATE = re.compile(
    r"(?<!\d)\d{4}-\d{2}-\d{2}(?!\d)"
    r"|\b(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|"
    r"jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|"
    r"nov(?:ember)?|dec(?:ember)?)\.?\s+\d{1,2}(?:st|nd|rd|th)?(?:,?\s+\d{4})?\b"
    r"|(?<!\d)\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?(?!\d)",
    re.IGNORECASE,
)
_TEMPORAL_CUE = re.compile(
    r"\b(?:today|tomorrow|tonight|yesterday|monday|tuesday|wednesday|"
    r"thursday|friday|saturday|sunday|next\s+(?:day|week|month)|"
    r"in\s+\d+\s+(?:hours?|days?|weeks?))\b",
    re.IGNORECASE,
)
_EVENT_CUE = re.compile(
    r"\b(?:appointment|booking|call|ceremony|conference|deadline|demo|due|"
    r"event|flight|interview|launch|meeting|offsite|presentation|renewal|"
    r"reservation|session|stay|trip|visit|workshop)\b",
    re.IGNORECASE,
)
_DEADLINE_CUE = re.compile(
    r"\b(?:deadline|due|expires?|no\s+later\s+than|complete\s+by|"
    r"submit\s+by|pay\s+by|renew\s+by|rsvp\s+by)\b",
    re.IGNORECASE,
)
_ICS_CUE = re.compile(
    r"BEGIN:VCALENDAR|\btext/calendar\b|\b[^\s()<>]+\.ics\b", re.IGNORECASE
)
_LIFECYCLE_PATTERNS = {
    "scheduled": re.compile(r"\b(?:booked|confirmed|scheduled)\b", re.IGNORECASE),
    "rescheduled": re.compile(r"\b(?:rescheduled|moved|new\s+time)\b", re.IGNORECASE),
    "cancelled": re.compile(r"\b(?:cancelled|canceled|called\s+off)\b", re.IGNORECASE),
    "completed": re.compile(
        r"\b(?:completed|concluded|ended|finished|took\s+place|was\s+held)\b",
        re.IGNORECASE,
    ),
}

_MONTH_NAME = (
    r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|"
    r"jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|"
    r"nov(?:ember)?|dec(?:ember)?)"
)
_TEMPORAL_FORM_PATTERNS = {
    "explicit_full_year": re.compile(
        rf"(?<![\d-])\d{{4}}-\d{{2}}-\d{{2}}(?![\d-])"
        rf"|\b{_MONTH_NAME}\.?\s+\d{{1,2}}(?:st|nd|rd|th)?,?\s+\d{{4}}\b"
        rf"|\b\d{{1,2}}(?:st|nd|rd|th)?\s+{_MONTH_NAME}\.?,?\s+\d{{4}}\b",
        re.IGNORECASE,
    ),
    "inferred_year_month_day": re.compile(
        rf"\b{_MONTH_NAME}\.?\s+\d{{1,2}}(?:st|nd|rd|th)?\b"
        rf"(?!\s*,?\s*\d{{4}}\b)"
        rf"|\b\d{{1,2}}(?:st|nd|rd|th)?\s+{_MONTH_NAME}\.?\b"
        rf"(?!\s*,?\s*\d{{4}}\b)",
        re.IGNORECASE,
    ),
    "numeric": re.compile(
        r"(?<![\d/-])(?:\d{4}[/.]\d{1,2}[/.]\d{1,2}"
        r"|\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?)(?![\d/-])"
    ),
    "relative_or_weekday": re.compile(
        r"\b(?:today|tomorrow|tonight|yesterday|"
        r"(?:(?:this|this\s+coming|next|last)\s+)?"
        r"(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)|"
        r"next\s+(?:day|week|month)|in\s+\d+\s+(?:hours?|days?|weeks?))\b",
        re.IGNORECASE,
    ),
}
_CLOCK_FORM = re.compile(
    r"(?<![\d:])(?:[01]?\d|2[0-3])(?::[0-5]\d)?\s*"
    r"(?:a\.?m\.?|p\.?m\.?)\b"
    r"|(?<![\d:])(?:[01]?\d|2[0-3]):[0-5]\d"
    r"(?:\s*(?:Z|UTC|GMT|[+-]\d{2}:?\d{2}|"
    r"PST|PDT|MST|MDT|CST|CDT|EST|EDT))\b",
    re.IGNORECASE,
)
_TEMPORAL_FORM_NAMES = (
    "explicit_full_year",
    "inferred_year_month_day",
    "numeric",
    "relative_or_weekday",
    "time_only",
)
_NOISE_CLASSES = (
    "advertising_or_bulk",
    "routine",
    "fact_eligible_signal",
    "other_suppressed",
)
_CALIBRATION_STRATA = (
    "direct_hit",
    "important_temporal_miss",
    "explicit_proxy_miss",
    "human_mail_lead",
    "lifecycle_language",
    "bulk_advertising_negative",
)
_ACTION_TEMPORAL_CUE = re.compile(
    r"\b(?:apply|complete|file|finish|pay|register|reply|respond|send|submit)\b"
    r"|\b(?:action\s+required|rsvp)\b",
    re.IGNORECASE,
)


class HistoricalGmailEvaluationError(ValueError):
    """Raised when a historical projection cannot be evaluated safely."""


@dataclass(frozen=True)
class HistoricalGmailGateConfig:
    """Pragmatic replay gates; labels remain distinct from historical proxies."""

    minimum_historical_coverage: float = 1.0
    minimum_supported_time_precision: float = 0.95
    minimum_explicit_date_recall_proxy: float = 0.90
    minimum_important_temporal_detection_proxy: float = 0.85
    maximum_fact_eligible_rate_delta: float = 0.02
    minimum_final_judge_acceptance: float = 0.95
    minimum_human_temporal_recall: float = 0.85
    minimum_labeled_sample_size: int = 100
    minimum_labeled_per_required_stratum: int = 5
    required_calibration_strata: tuple[str, ...] = _CALIBRATION_STRATA
    require_zero_critical_errors: bool = True
    require_zero_privacy_violations: bool = True
    require_zero_cross_occurrence_errors: bool = True
    require_zero_unintended_writes: bool = True
    require_deterministic_replay: bool = True

    def __post_init__(self) -> None:
        for value in (
            self.minimum_historical_coverage,
            self.minimum_supported_time_precision,
            self.minimum_explicit_date_recall_proxy,
            self.minimum_important_temporal_detection_proxy,
            self.maximum_fact_eligible_rate_delta,
            self.minimum_final_judge_acceptance,
            self.minimum_human_temporal_recall,
        ):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise HistoricalGmailEvaluationError("gate values must be numeric")
            if not 0 <= float(value) <= 1:
                raise HistoricalGmailEvaluationError(
                    "gate values must be between zero and one"
                )
        for value in (
            self.minimum_labeled_sample_size,
            self.minimum_labeled_per_required_stratum,
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise HistoricalGmailEvaluationError(
                    "labeled sample gates must be positive integers"
                )
        if (
            not isinstance(self.required_calibration_strata, tuple)
            or not self.required_calibration_strata
            or len(set(self.required_calibration_strata))
            != len(self.required_calibration_strata)
            or any(
                value not in _CALIBRATION_STRATA
                for value in self.required_calibration_strata
            )
        ):
            raise HistoricalGmailEvaluationError(
                "required_calibration_strata must be a non-empty unique supported tuple"
            )


@dataclass(frozen=True)
class _Revision:
    lineage: str
    revision_key: str
    variant_key: str
    sort_key: tuple[Any, ...]
    projection_version: int
    classifier_version: int
    fact_eligible: bool
    deleted: bool
    delivery_kind: str
    fact_importance: str
    actionability: str
    evidence_text: str
    message_ranges: tuple[_MessageEvidence, ...]
    trusted_message_ranges_available: bool
    structured_ics_available: bool
    evidence_digest: str


@dataclass(frozen=True)
class _MessageEvidence:
    text: str
    internal_at: str
    subject: str
    body: str


@dataclass(frozen=True)
class _EvaluatedRevision:
    lineage: str
    revision_key: str
    sort_key: tuple[Any, ...]
    sample_id: str
    fact_eligible: bool
    deleted: bool
    delivery_kind: str
    fact_importance: str
    actionability: str
    temporal_evidence: bool
    explicit_date_evidence: bool
    parsed: bool
    normalized: bool
    event: bool
    deadline: bool
    deadline_evidence_proxy: bool
    ambiguous: bool
    held: bool
    structured_ics_available: bool
    trusted_message_ranges_available: bool
    lifecycle_cues: tuple[str, ...]
    temporal_forms: tuple[str, ...]
    temporal_form_location: str
    temporal_cue_evidence: bool
    cue_association_gap: bool
    candidate_counts: tuple[tuple[str, str, str, tuple[str, ...]], ...]
    evidence_digest: str
    candidate_digest: str
    detector_error: bool
    nondeterministic: bool
    annotation: Mapping[str, Any]


Detector = Callable[..., Sequence[Any]]


def evaluate_historical_gmail_projection(
    projection_root: Path,
    *,
    sample_secret: bytes | str,
    detector: Detector | None = None,
    gates: HistoricalGmailGateConfig | None = None,
    expected_file_count: int | None = None,
    baseline_fact_eligible_rate: float | None = None,
    annotations: Mapping[str, Mapping[str, Any]] | None = None,
    calibration_cohort: Sequence[str] | None = None,
    max_sample_ids: int = 20,
    verify_detector_determinism: bool = True,
) -> dict[str, Any]:
    """Evaluate immutable Gmail projections without emitting mailbox content.

    ``annotations`` is keyed by opaque sample IDs emitted by an earlier pass.
    ``calibration_cohort`` freezes the sampled IDs before labeling; gold gates use
    only that cohort and require complete labels.  On parsed records, ``supported``
    means every emitted candidate time was reviewed and supported.  This avoids
    selective favorable annotations without putting provider IDs in the artifact.
    """

    root = Path(projection_root)
    if not root.is_dir():
        raise HistoricalGmailEvaluationError("projection_root must be a directory")
    secret = _sample_secret(sample_secret)
    if isinstance(max_sample_ids, bool) or not 0 <= max_sample_ids <= 100:
        raise HistoricalGmailEvaluationError(
            "max_sample_ids must be between zero and 100"
        )
    if expected_file_count is not None and (
        isinstance(expected_file_count, bool) or expected_file_count <= 0
    ):
        raise HistoricalGmailEvaluationError("expected_file_count must be positive")
    if baseline_fact_eligible_rate is not None and not (
        0 <= baseline_fact_eligible_rate <= 1
    ):
        raise HistoricalGmailEvaluationError(
            "baseline_fact_eligible_rate must be between zero and one"
        )

    selected_detector = detector or _default_detector
    gate_config = gates or HistoricalGmailGateConfig()
    annotation_map = annotations or {}
    paths = sorted(root.rglob("*.md"), key=lambda item: str(item.relative_to(root)))
    revisions: list[_Revision] = []
    invalid_files = 0
    non_gmail_files = 0
    processed_files = 0
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
            processed_files += 1
            local_tie_break = _internal_digest(
                "projection_path", str(path.relative_to(root))
            )
            revision = _revision_from_markdown(text, local_tie_break=local_tie_break)
        except (OSError, TypeError, ValueError):
            invalid_files += 1
            continue
        if revision is None:
            non_gmail_files += 1
            continue
        revisions.append(revision)

    variants: dict[str, _Revision] = {}
    for revision in revisions:
        existing = variants.get(revision.variant_key)
        if existing is None or revision.sort_key > existing.sort_key:
            variants[revision.variant_key] = revision
    unique: dict[str, _Revision] = {}
    for revision in variants.values():
        existing = unique.get(revision.revision_key)
        if existing is None or revision.sort_key > existing.sort_key:
            unique[revision.revision_key] = revision
    unique_revisions = sorted(
        unique.values(),
        key=lambda item: (item.lineage, item.sort_key, item.revision_key),
    )
    evaluated = [
        _evaluate_revision(
            revision,
            secret=secret,
            detector=selected_detector,
            annotations=annotation_map,
            verify_determinism=verify_detector_determinism,
        )
        for revision in unique_revisions
    ]

    by_lineage: dict[str, list[_EvaluatedRevision]] = defaultdict(list)
    for revision in evaluated:
        by_lineage[revision.lineage].append(revision)
    for values in by_lineage.values():
        values.sort(key=lambda item: (item.sort_key, item.revision_key))
    latest_all = [values[-1] for values in by_lineage.values()]
    latest_active = [item for item in latest_all if not item.deleted]
    calibration_records, calibration_summary = _resolve_calibration_cohort(
        evaluated,
        calibration_cohort=calibration_cohort,
        required_strata=gate_config.required_calibration_strata,
    )

    discovered = len(paths)
    expected = expected_file_count or discovered
    source_coverage = min(1.0, len(revisions) / expected) if expected else 1.0
    detector_successes = sum(
        not item.detector_error
        and (item.trusted_message_ranges_available or item.deleted)
        for item in evaluated
    )
    detector_coverage = (
        detector_successes / len(evaluated)
        if evaluated
        else (1.0 if not revisions else 0.0)
    )
    historical_coverage = min(source_coverage, detector_coverage)

    current_summary = _summarize(latest_active, max_sample_ids=max_sample_ids)
    historical_summary = _summarize(evaluated, max_sample_ids=max_sample_ids)
    transition_summary = _transitions(by_lineage)
    metrics = _quality_metrics(
        evaluated,
        latest_active,
        calibration_records=calibration_records,
        calibration_summary=calibration_summary,
        historical_coverage=historical_coverage,
        baseline_fact_eligible_rate=baseline_fact_eligible_rate,
    )
    gate_report = _evaluate_gates(metrics, gate_config)
    report: dict[str, Any] = {
        "schema_version": GMAIL_TEMPORAL_EVALUATION_SCHEMA_VERSION,
        "coverage": {
            "files_discovered": discovered,
            "files_processed": processed_files,
            "expected_files": expected,
            "gmail_projection_files": len(revisions),
            "non_gmail_files": non_gmail_files,
            "invalid_files": invalid_files,
            "unique_revisions": len(evaluated),
            "duplicate_projection_files": len(revisions) - len(variants),
            "collapsed_projection_variants": len(variants) - len(evaluated),
            "opaque_thread_lineages": len(by_lineage),
            "current_active_threads": len(latest_active),
            "current_deleted_threads": len(latest_all) - len(latest_active),
            "source_coverage_rate": source_coverage,
            "detector_coverage_rate": detector_coverage,
            "historical_coverage_rate": historical_coverage,
        },
        "current_latest_per_thread": current_summary,
        "historical_unique_revisions": historical_summary,
        "historical_transitions": transition_summary,
        "calibration_cohort": calibration_summary,
        "quality_metrics": metrics,
        "gates": gate_report,
        "privacy": {
            "aggregate_only": True,
            "opaque_thread_grouping": True,
            "stable_keyed_sample_ids": True,
            "titles_bodies_addresses_provider_ids_emitted": 0,
            "assertion_passed": True,
        },
    }
    _assert_aggregate_report(report)
    return report


def _revision_from_markdown(text: str, *, local_tie_break: str) -> _Revision | None:
    frontmatter, _untrusted_body = parse_frontmatter(text)
    if not isinstance(frontmatter, dict):
        raise HistoricalGmailEvaluationError("invalid projection frontmatter")
    if str(frontmatter.get("source_type") or "") != "gmail_thread":
        return None
    account = _private_text(frontmatter.get("gmail_account_key"))
    thread_id = _private_text(frontmatter.get("gmail_thread_id"))
    revision = _private_text(frontmatter.get("gmail_source_revision"))
    if not account or not thread_id or not revision:
        raise HistoricalGmailEvaluationError("Gmail projection lineage is incomplete")
    body = strip_frontmatter(text)
    projection_version = _projection_version(frontmatter, "gmail_projection_version")
    classifier_version = _projection_version(frontmatter, "gmail_classifier_version")
    lineage = _internal_digest("lineage", account, thread_id)
    revision_key = _internal_digest("revision", account, thread_id, revision)
    variant_key = _internal_digest(
        "variant",
        account,
        thread_id,
        revision,
        str(projection_version),
        str(classifier_version),
    )
    sort_key = (
        *_timestamp_rank(frontmatter.get("archive_updated_at")),
        projection_version,
        *_timestamp_rank(frontmatter.get("captured_at")),
        revision,
        local_tie_break,
    )
    ranges = _trusted_message_ranges(frontmatter, body)
    trusted_message_ranges_available = ranges is not None
    evidence_text = "\n\n".join(value.text for value in ranges or ())
    return _Revision(
        lineage=lineage,
        revision_key=revision_key,
        variant_key=variant_key,
        sort_key=sort_key,
        projection_version=projection_version,
        classifier_version=classifier_version,
        fact_eligible=_truthy(frontmatter.get("fact_eligible")),
        deleted=_truthy(frontmatter.get("deleted")),
        delivery_kind=_dimension(frontmatter.get("delivery_kind"), _DELIVERY_KINDS),
        fact_importance=_dimension(
            frontmatter.get("fact_importance"), _IMPORTANCE_KINDS
        ),
        actionability=_dimension(
            frontmatter.get("actionability"), _ACTIONABILITY_KINDS
        ),
        evidence_text=evidence_text,
        message_ranges=tuple(ranges or ()),
        trusted_message_ranges_available=trusted_message_ranges_available,
        structured_ics_available=bool(_ICS_CUE.search(body)),
        evidence_digest=_internal_digest("source_evidence", evidence_text),
    )


def _trusted_message_ranges(
    frontmatter: Mapping[str, Any], body: str
) -> tuple[_MessageEvidence, ...] | None:
    """Fail closed unless the connector-authored range index is internally exact."""

    if _strict_int(frontmatter.get("gmail_message_timestamps_version")) != 1:
        return None
    message_ids = frontmatter.get("gmail_message_ids")
    entries = frontmatter.get("gmail_message_timestamps")
    retained_count = _strict_int(frontmatter.get("retained_message_count"))
    if not isinstance(message_ids, list) or not isinstance(entries, list):
        return None
    if retained_count is None or retained_count != len(message_ids):
        return None
    if len(entries) != len(message_ids) or len(set(message_ids)) != len(message_ids):
        return None
    if any(
        not isinstance(message_id, str)
        or not _PROVIDER_MESSAGE_ID.fullmatch(message_id)
        for message_id in message_ids
    ):
        return None
    expected_keys = {"message_id", "internal_date", "start_offset", "end_offset"}
    ranges: list[_MessageEvidence] = []
    previous_end = -1
    for index, (message_id, entry) in enumerate(zip(message_ids, entries), start=1):
        if not isinstance(entry, dict) or set(entry) != expected_keys:
            return None
        if entry.get("message_id") != message_id:
            return None
        internal_at = entry.get("internal_date")
        if not isinstance(internal_at, str) or not _is_aware_datetime(internal_at):
            return None
        start = _strict_int(entry.get("start_offset"))
        end = _strict_int(entry.get("end_offset"))
        if (
            start is None
            or end is None
            or start < 0
            or end <= start
            or end > len(body)
            or start <= previous_end
        ):
            return None
        if index == 1:
            if not body.startswith("# Email thread:") or not body[:start].endswith(
                "\n\n"
            ):
                return None
        elif body[previous_end:start] != "\n\n":
            return None
        rendered_message = body[start:end]
        first_line = rendered_message.split("\n", 1)[0]
        if first_line != f"## Message {index} — {internal_at} — {message_id}":
            return None
        evidence = _message_source_evidence(rendered_message, internal_at=internal_at)
        if evidence is None:
            return None
        ranges.append(evidence)
        previous_end = end
    if ranges and previous_end != len(body):
        return None
    return tuple(ranges)


def _message_source_evidence(
    rendered_message: str, *, internal_at: str
) -> _MessageEvidence | None:
    """Keep sender-controlled subject/body, excluding connector metadata clocks."""

    evidence = gmail_message_source_evidence(rendered_message)
    if evidence is None:
        return None
    return _MessageEvidence(
        text=evidence.text,
        internal_at=internal_at,
        subject=evidence.subject,
        body=evidence.body,
    )


def _temporal_forms(value: str) -> tuple[str, ...]:
    forms = [
        name
        for name, pattern in _TEMPORAL_FORM_PATTERNS.items()
        if pattern.search(value)
    ]
    if _CLOCK_FORM.search(value) and not forms:
        forms.append("time_only")
    return tuple(forms)


def _temporal_form_diagnostic(
    messages: Sequence[_MessageEvidence],
) -> tuple[tuple[str, ...], str]:
    forms: set[str] = set()
    subject_bearing = False
    body_bearing = False
    for message in messages:
        subject_forms = _temporal_forms(message.subject)
        body_forms = _temporal_forms(message.body)
        forms.update(subject_forms)
        forms.update(body_forms)
        subject_bearing = subject_bearing or bool(subject_forms)
        body_bearing = body_bearing or bool(body_forms)
    if subject_bearing and body_bearing:
        location = "subject_and_body"
    elif subject_bearing:
        location = "subject_only"
    elif body_bearing:
        location = "body_only"
    else:
        location = "none"
    return tuple(name for name in _TEMPORAL_FORM_NAMES if name in forms), location


def _is_aware_datetime(value: str) -> bool:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


def _evaluate_revision(
    revision: _Revision,
    *,
    secret: bytes,
    detector: Detector,
    annotations: Mapping[str, Mapping[str, Any]],
    verify_determinism: bool,
) -> _EvaluatedRevision:
    sample_id = _sample_id(secret, revision.revision_key)
    candidate_rows: list[tuple[str, str, str, tuple[str, ...]]] = []
    candidate_fingerprints: list[tuple[Any, ...]] = []
    detector_error = False
    nondeterministic = False
    for index, message in enumerate(revision.message_ranges):
        try:
            first = detector(
                text=message.text,
                message_internal_at=message.internal_at or None,
                chunk_id=f"{sample_id}-m{index + 1}",
            )
            first_rows = _candidate_rows(first)
            first_fingerprint = _candidate_fingerprint(first)
            candidate_fingerprints.extend(
                _candidate_transition_fingerprint(first_fingerprint)
            )
            if verify_determinism:
                second = detector(
                    text=message.text,
                    message_internal_at=message.internal_at or None,
                    chunk_id=f"{sample_id}-m{index + 1}",
                )
                nondeterministic = nondeterministic or (
                    first_fingerprint != _candidate_fingerprint(second)
                )
            candidate_rows.extend(first_rows)
        except Exception:
            detector_error = True
    candidates = tuple(candidate_rows)
    parsed = bool(candidates)
    normalized = bool(candidates) and all(
        row[0] in _RELATIONS and row[1] in _KINDS and row[2] in _PRECISIONS
        for row in candidates
    )
    same_message_temporal_evidence = any(
        _EVENT_CUE.search(message.text)
        and (_EXPLICIT_DATE.search(message.text) or _TEMPORAL_CUE.search(message.text))
        for message in revision.message_ranges
    )
    temporal_evidence = bool(
        parsed or revision.structured_ics_available or same_message_temporal_evidence
    )
    explicit_date_evidence = any(
        _EVENT_CUE.search(message.text) and _EXPLICIT_DATE.search(message.text)
        for message in revision.message_ranges
    )
    temporal_forms, temporal_form_location = _temporal_form_diagnostic(
        revision.message_ranges
    )
    temporal_cue_evidence = bool(
        _EVENT_CUE.search(revision.evidence_text)
        or _DEADLINE_CUE.search(revision.evidence_text)
        or _ACTION_TEMPORAL_CUE.search(revision.evidence_text)
    )
    cue_association_gap = bool(
        not parsed
        and any(
            _temporal_forms(message.text)
            and (
                _EVENT_CUE.search(message.text)
                or _DEADLINE_CUE.search(message.text)
                or _ACTION_TEMPORAL_CUE.search(message.text)
            )
            for message in revision.message_ranges
        )
    )
    unavailable_important_temporal = bool(
        revision.fact_eligible
        and revision.fact_importance == "important_temporal"
        and not revision.trusted_message_ranges_available
    )
    ambiguous = bool(
        (temporal_evidence and (not parsed or not normalized))
        or unavailable_important_temporal
    )
    held = bool(revision.fact_eligible and ambiguous)
    lifecycle = tuple(
        name
        for name, pattern in _LIFECYCLE_PATTERNS.items()
        if pattern.search(revision.evidence_text)
    )
    annotation = annotations.get(sample_id)
    if not isinstance(annotation, Mapping):
        annotation = {}
    return _EvaluatedRevision(
        lineage=revision.lineage,
        revision_key=revision.revision_key,
        sort_key=revision.sort_key,
        sample_id=sample_id,
        fact_eligible=revision.fact_eligible,
        deleted=revision.deleted,
        delivery_kind=revision.delivery_kind,
        fact_importance=revision.fact_importance,
        actionability=revision.actionability,
        temporal_evidence=temporal_evidence,
        explicit_date_evidence=explicit_date_evidence,
        parsed=parsed,
        normalized=normalized,
        event=any(row[0] == "occurrence" for row in candidates),
        deadline=any(row[0] == "deadline" for row in candidates),
        deadline_evidence_proxy=any(
            _DEADLINE_CUE.search(message.text)
            and (
                _EXPLICIT_DATE.search(message.text)
                or _TEMPORAL_CUE.search(message.text)
            )
            for message in revision.message_ranges
        ),
        ambiguous=ambiguous,
        held=held,
        structured_ics_available=revision.structured_ics_available,
        trusted_message_ranges_available=(revision.trusted_message_ranges_available),
        lifecycle_cues=lifecycle,
        temporal_forms=temporal_forms,
        temporal_form_location=temporal_form_location,
        temporal_cue_evidence=temporal_cue_evidence,
        cue_association_gap=cue_association_gap,
        candidate_counts=candidates,
        evidence_digest=revision.evidence_digest,
        candidate_digest=_internal_digest(
            "candidate_state",
            json.dumps(candidate_fingerprints, ensure_ascii=True, default=str),
        ),
        detector_error=detector_error,
        nondeterministic=nondeterministic,
        annotation=dict(annotation),
    )


def _candidate_rows(
    values: Sequence[Any],
) -> list[tuple[str, str, str, tuple[str, ...]]]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise HistoricalGmailEvaluationError("detector output must be a sequence")
    rows: list[tuple[str, str, str, tuple[str, ...]]] = []
    for value in values:
        getter = (
            value.get
            if isinstance(value, Mapping)
            else lambda name, default=None: getattr(value, name, default)
        )
        relation = _dimension(getter("relation"), _RELATIONS)
        kind = _dimension(getter("kind"), _KINDS)
        precision = _dimension(getter("precision"), _PRECISIONS)
        raw_bases = getter("resolution_basis", ())
        if isinstance(raw_bases, str):
            raw_bases = (raw_bases,)
        bases = tuple(sorted({_resolution_basis(item) for item in raw_bases or ()}))
        rows.append((relation, kind, precision, bases))
    return rows


def _candidate_fingerprint(values: Sequence[Any]) -> tuple[tuple[Any, ...], ...]:
    """Compare private detector details without carrying them into the report."""

    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise HistoricalGmailEvaluationError("detector output must be a sequence")
    output: list[tuple[Any, ...]] = []
    for value in values:
        getter = (
            value.get
            if isinstance(value, Mapping)
            else lambda name, default=None: getattr(value, name, default)
        )
        output.append(
            (
                str(getter("relation") or ""),
                str(getter("kind") or ""),
                str(getter("start_at") or ""),
                str(getter("end_at") or ""),
                str(getter("precision") or ""),
                tuple(str(item) for item in getter("resolution_basis", ()) or ()),
                _span_fingerprint(getter("expression_span")),
                _span_fingerprint(getter("cue_span")),
            )
        )
    return tuple(output)


def _span_fingerprint(value: Any) -> tuple[Any, Any, Any]:
    if isinstance(value, Mapping):
        return (value.get("start"), value.get("end"), value.get("chunk_id"))
    return (
        getattr(value, "start", None),
        getattr(value, "end", None),
        getattr(value, "chunk_id", None),
    )


def _candidate_transition_fingerprint(
    values: Sequence[tuple[Any, ...]],
) -> tuple[tuple[Any, ...], ...]:
    """Drop revision-scoped chunk IDs before comparing candidate lifecycle state."""

    output: list[tuple[Any, ...]] = []
    for value in values:
        expression_span = value[6]
        cue_span = value[7]
        output.append(
            (
                *value[:6],
                (expression_span[0], expression_span[1]),
                (cue_span[0], cue_span[1]),
            )
        )
    return tuple(output)


def _summarize(
    records: Sequence[_EvaluatedRevision], *, max_sample_ids: int
) -> dict[str, Any]:
    funnel_names = (
        "eligible",
        "temporal_evidence_bearing",
        "parsed",
        "normalized",
        "event",
        "deadline",
        "deadline_evidence_proxy",
        "ambiguous",
        "held",
        "structured_ics_available",
        "trusted_message_ranges_available",
        "lifecycle_cue_bearing",
    )
    funnel = {"total": len(records)}
    attributes = {
        "eligible": "fact_eligible",
        "temporal_evidence_bearing": "temporal_evidence",
        "lifecycle_cue_bearing": "lifecycle_cues",
    }
    for name in funnel_names:
        attribute = attributes.get(name, name)
        funnel[name] = sum(bool(getattr(record, attribute)) for record in records)

    dimensions: dict[str, Counter[str]] = {
        "relation": Counter(),
        "kind": Counter(),
        "precision": Counter(),
        "resolution_basis": Counter(),
        "lifecycle": Counter(),
        "revision_noise_class": Counter(),
    }
    strata: Counter[tuple[str, str, str, bool]] = Counter()
    noise_yield: dict[str, Counter[str]] = {name: Counter() for name in _NOISE_CLASSES}
    for record in records:
        noise = _noise_class(record)
        dimensions["revision_noise_class"][noise] += 1
        noise_yield[noise]["revisions"] += 1
        noise_yield[noise]["parsed_revisions"] += int(record.parsed)
        noise_yield[noise]["normalized_revisions"] += int(record.normalized)
        noise_yield[noise]["candidates"] += len(record.candidate_counts)
        for lifecycle in record.lifecycle_cues:
            dimensions["lifecycle"][lifecycle] += 1
        for relation, kind, precision, bases in record.candidate_counts:
            dimensions["relation"][relation] += 1
            dimensions["kind"][kind] += 1
            dimensions["precision"][precision] += 1
            dimensions["resolution_basis"].update(bases or ("none",))
        strata[
            (
                record.delivery_kind,
                record.fact_importance,
                record.actionability,
                record.fact_eligible,
            )
        ] += 1
    samples: dict[str, list[str]] = {}
    for name, predicate in (
        ("temporal_evidence", lambda item: item.temporal_evidence),
        ("parse_miss", lambda item: item.temporal_evidence and not item.parsed),
        ("normalization_miss", lambda item: item.parsed and not item.normalized),
        ("important_temporal_miss", _important_temporal_miss),
        ("ambiguous", lambda item: item.ambiguous),
        ("held", lambda item: item.held),
        ("structured_ics", lambda item: item.structured_ics_available),
        (
            "trusted_message_range_unavailable",
            lambda item: not item.trusted_message_ranges_available,
        ),
        ("detector_error", lambda item: item.detector_error),
    ):
        samples[name] = sorted({item.sample_id for item in records if predicate(item)})[
            :max_sample_ids
        ]
    for name in _CALIBRATION_STRATA:
        samples[f"calibration_{name}"] = sorted(
            {item.sample_id for item in records if name in _calibration_strata(item)}
        )[:max_sample_ids]
    return {
        "funnel": funnel,
        "candidate_dimensions": {
            name: dict(sorted(counter.items())) for name, counter in dimensions.items()
        },
        "classifier_noise_strata": [
            {
                "delivery_kind": delivery,
                "fact_importance": importance,
                "actionability": actionability,
                "fact_eligible": eligible,
                "count": count,
            }
            for (delivery, importance, actionability, eligible), count in sorted(
                strata.items()
            )
        ],
        "detector_yield_by_noise_class": {
            name: {
                "revisions": noise_yield[name]["revisions"],
                "parsed_revisions": noise_yield[name]["parsed_revisions"],
                "parsed_revision_rate": (
                    noise_yield[name]["parsed_revisions"]
                    / noise_yield[name]["revisions"]
                    if noise_yield[name]["revisions"]
                    else None
                ),
                "normalized_revisions": noise_yield[name]["normalized_revisions"],
                "candidates": noise_yield[name]["candidates"],
            }
            for name in _NOISE_CLASSES
        },
        "important_temporal_miss_diagnostics": _important_temporal_miss_diagnostics(
            records
        ),
        "sample_ids": samples,
    }


def _important_temporal_miss(record: _EvaluatedRevision) -> bool:
    return record.fact_importance == "important_temporal" and not record.parsed


def _important_temporal_miss_diagnostics(
    records: Sequence[_EvaluatedRevision],
) -> dict[str, Any]:
    misses = [record for record in records if _important_temporal_miss(record)]
    forms = {
        name: sum(name in record.temporal_forms for record in misses)
        for name in _TEMPORAL_FORM_NAMES
    }
    locations = {
        name: sum(record.temporal_form_location == name for record in misses)
        for name in ("subject_only", "body_only", "subject_and_body", "none")
    }
    form_bearing = sum(bool(record.temporal_forms) for record in misses)
    return {
        "basis": "historical_classifier_proxy_not_gold_overlapping_strata",
        "misses": len(misses),
        "by_temporal_form": forms,
        "temporal_form_bearing": form_bearing,
        "no_temporal_form": len(misses) - form_bearing,
        "cue_association": {
            "cue_present_unassociated": sum(
                record.cue_association_gap for record in misses
            ),
            "temporal_form_without_cue": sum(
                bool(record.temporal_forms) and not record.cue_association_gap
                for record in misses
            ),
            "no_temporal_form": len(misses) - form_bearing,
        },
        "structured_ics_available": sum(
            record.structured_ics_available for record in misses
        ),
        "trusted_message_ranges_unavailable": sum(
            not record.trusted_message_ranges_available for record in misses
        ),
        "temporal_form_location": locations,
        "detector_support": {
            "explicit_full_year": "full_except_numeric_dates",
            "inferred_year_month_day": "full_with_trusted_message_clock",
            "numeric": "not_supported",
            "relative_or_weekday": "today_tomorrow_this_coming_weekday_only",
            "time_only": "not_supported_without_date",
        },
    }


def _resolve_calibration_cohort(
    records: Sequence[_EvaluatedRevision],
    *,
    calibration_cohort: Sequence[str] | None,
    required_strata: Sequence[str],
) -> tuple[list[_EvaluatedRevision], dict[str, Any]]:
    if calibration_cohort is None:
        return [], {
            "provided": False,
            "manifest_sha256": None,
            "frozen_records": 0,
            "parsed_records": 0,
            "fully_annotated_records": 0,
            "annotation_coverage_rate": None,
            "required_strata": list(required_strata),
            "stratum_counts": {name: 0 for name in _CALIBRATION_STRATA},
        }
    if isinstance(calibration_cohort, (str, bytes)) or not isinstance(
        calibration_cohort, Sequence
    ):
        raise HistoricalGmailEvaluationError(
            "calibration_cohort must be a sequence of opaque sample IDs"
        )
    supplied = [str(value).strip() for value in calibration_cohort]
    if any(not value for value in supplied):
        raise HistoricalGmailEvaluationError(
            "calibration_cohort contains an empty sample ID"
        )
    if len(set(supplied)) != len(supplied):
        raise HistoricalGmailEvaluationError(
            "calibration_cohort sample IDs must be unique"
        )
    by_sample_id = {record.sample_id: record for record in records}
    unknown = [sample_id for sample_id in supplied if sample_id not in by_sample_id]
    if unknown:
        raise HistoricalGmailEvaluationError(
            "calibration_cohort contains unknown or stale sample IDs"
        )
    selected = [by_sample_id[sample_id] for sample_id in supplied]
    stratum_counts = {
        name: sum(name in _calibration_strata(record) for record in selected)
        for name in _CALIBRATION_STRATA
    }
    fully_annotated = sum(_calibration_annotation_complete(item) for item in selected)
    return selected, {
        "provided": True,
        "manifest_sha256": _internal_digest(
            "calibration_cohort_manifest_v1", *sorted(supplied)
        ),
        "frozen_records": len(selected),
        "parsed_records": sum(item.parsed for item in selected),
        "fully_annotated_records": fully_annotated,
        "annotation_coverage_rate": (
            fully_annotated / len(selected) if selected else 0.0
        ),
        "required_strata": list(required_strata),
        "stratum_counts": stratum_counts,
    }


def _calibration_strata(record: _EvaluatedRevision) -> set[str]:
    strata: set[str] = set()
    if record.parsed:
        strata.add("direct_hit")
    if _important_temporal_miss(record):
        strata.add("important_temporal_miss")
    if record.fact_eligible and record.explicit_date_evidence and not record.parsed:
        strata.add("explicit_proxy_miss")
    if record.delivery_kind in {"human", "mixed"} or record.fact_eligible:
        strata.add("human_mail_lead")
    if record.lifecycle_cues:
        strata.add("lifecycle_language")
    if record.delivery_kind == "bulk" or record.fact_importance == "advertising":
        strata.add("bulk_advertising_negative")
    return strata


def _calibration_annotation_complete(record: _EvaluatedRevision) -> bool:
    required = ("temporal_relevant", "critical_error", "cross_occurrence_error")
    if record.parsed:
        required += ("supported", "final_judge_acceptable")
    return all(
        _annotation_bool(record.annotation, name) is not None for name in required
    )


def _transitions(
    by_lineage: Mapping[str, Sequence[_EvaluatedRevision]],
) -> dict[str, int]:
    counts: Counter[str] = Counter()
    counts["threads_with_multiple_revisions"] = sum(
        len(values) > 1 for values in by_lineage.values()
    )
    counts["threads_with_evidence_content_variation"] = sum(
        len({item.evidence_digest for item in values}) > 1
        for values in by_lineage.values()
    )
    for values in by_lineage.values():
        for previous, current in zip(values, values[1:]):
            counts["revision_transitions"] += 1
            evidence_changed = previous.evidence_digest != current.evidence_digest
            counts[
                "evidence_content_changed"
                if evidence_changed
                else "evidence_content_unchanged"
            ] += 1
            _transition(
                counts, "eligibility", previous.fact_eligible, current.fact_eligible
            )
            _transition(
                counts,
                "temporal_evidence",
                previous.temporal_evidence,
                current.temporal_evidence,
            )
            _transition(counts, "parsed", previous.parsed, current.parsed)
            _transition(counts, "deleted", previous.deleted, current.deleted)
            if (previous.event, previous.deadline) != (current.event, current.deadline):
                counts["relation_changed"] += 1
            if previous.lifecycle_cues != current.lifecycle_cues:
                counts["lifecycle_cues_changed"] += 1
                if evidence_changed:
                    counts["lifecycle_cues_changed_with_evidence_content"] += 1
            if previous.candidate_digest != current.candidate_digest:
                counts["candidate_temporal_assignment_changed"] += 1
                if evidence_changed:
                    counts[
                        "candidate_temporal_assignment_changed_with_evidence_content"
                    ] += 1
            if evidence_changed and previous.parsed != current.parsed:
                counts["parsed_changed_with_evidence_content"] += 1
            if (
                evidence_changed
                and previous.temporal_evidence != current.temporal_evidence
            ):
                counts["temporal_evidence_changed_with_evidence_content"] += 1
    for name in (
        "revision_transitions",
        "threads_with_multiple_revisions",
        "threads_with_evidence_content_variation",
        "evidence_content_changed",
        "evidence_content_unchanged",
        "eligibility_changed",
        "eligibility_gained",
        "eligibility_lost",
        "temporal_evidence_changed",
        "temporal_evidence_gained",
        "temporal_evidence_lost",
        "parsed_changed",
        "parsed_gained",
        "parsed_lost",
        "deleted_changed",
        "deleted_gained",
        "deleted_lost",
        "relation_changed",
        "lifecycle_cues_changed",
        "lifecycle_cues_changed_with_evidence_content",
        "candidate_temporal_assignment_changed",
        "candidate_temporal_assignment_changed_with_evidence_content",
        "parsed_changed_with_evidence_content",
        "temporal_evidence_changed_with_evidence_content",
    ):
        counts[name] += 0
    return dict(sorted(counts.items()))


def _transition(counts: Counter[str], name: str, before: bool, after: bool) -> None:
    if before == after:
        return
    counts[f"{name}_changed"] += 1
    counts[f"{name}_{'gained' if after else 'lost'}"] += 1


def _quality_metrics(
    historical: Sequence[_EvaluatedRevision],
    current: Sequence[_EvaluatedRevision],
    *,
    calibration_records: Sequence[_EvaluatedRevision],
    calibration_summary: Mapping[str, Any],
    historical_coverage: float,
    baseline_fact_eligible_rate: float | None,
) -> dict[str, Any]:
    explicit_denominator = sum(
        item.fact_eligible and item.explicit_date_evidence for item in current
    )
    explicit_numerator = sum(
        item.fact_eligible and item.explicit_date_evidence and item.normalized
        for item in current
    )
    important_temporal = [
        item for item in current if item.fact_importance == "important_temporal"
    ]
    current_eligible_rate = (
        sum(item.fact_eligible for item in current) / len(current) if current else 0.0
    )
    cohort_provided = calibration_summary.get("provided") is True
    cohort_complete = bool(calibration_records) and (
        calibration_summary.get("annotation_coverage_rate") == 1.0
    )
    parsed_calibration = [item for item in calibration_records if item.parsed]
    temporal_relevant = [
        item
        for item in calibration_records
        if _annotation_bool(item.annotation, "temporal_relevant") is True
    ]
    stratum_counts = calibration_summary.get("stratum_counts") or {}
    required_strata = calibration_summary.get("required_strata") or ()
    minimum_stratum_count = (
        min(int(stratum_counts.get(name, 0)) for name in required_strata)
        if cohort_provided and required_strata
        else None
    )
    metrics = {
        "historical_coverage": _metric(historical_coverage, None, None, "measured"),
        "explicit_date_recall": _metric(
            explicit_numerator / explicit_denominator if explicit_denominator else None,
            explicit_numerator,
            explicit_denominator,
            "historical_proxy_not_gold",
        ),
        "current_important_temporal_detection": _metric(
            sum(item.parsed for item in important_temporal) / len(important_temporal)
            if important_temporal
            else None,
            sum(item.parsed for item in important_temporal),
            len(important_temporal),
            "historical_classifier_proxy_not_gold",
        ),
        "current_fact_eligible_rate": _metric(
            current_eligible_rate,
            sum(item.fact_eligible for item in current),
            len(current),
            "measured",
        ),
        "fact_eligible_rate_delta": _metric(
            abs(current_eligible_rate - baseline_fact_eligible_rate)
            if baseline_fact_eligible_rate is not None
            else None,
            None,
            None,
            "baseline_comparison"
            if baseline_fact_eligible_rate is not None
            else "not_available",
        ),
        "calibration_sample_size": _metric(
            len(calibration_records) if cohort_provided else None,
            len(calibration_records) if cohort_provided else None,
            None,
            "frozen_opaque_cohort" if cohort_provided else "not_available",
        ),
        "calibration_annotation_coverage": _metric(
            calibration_summary.get("annotation_coverage_rate")
            if cohort_provided
            else None,
            calibration_summary.get("fully_annotated_records")
            if cohort_provided
            else None,
            len(calibration_records) if cohort_provided else None,
            "complete_record_labels" if cohort_provided else "not_available",
        ),
        "calibration_minimum_stratum_count": _metric(
            minimum_stratum_count,
            None,
            None,
            "deterministic_overlapping_strata" if cohort_provided else "not_available",
        ),
        "human_temporal_recall": _metric(
            sum(
                item.parsed and _annotation_bool(item.annotation, "supported") is True
                for item in temporal_relevant
            )
            / len(temporal_relevant)
            if cohort_complete and temporal_relevant
            else None,
            sum(
                item.parsed and _annotation_bool(item.annotation, "supported") is True
                for item in temporal_relevant
            ),
            len(temporal_relevant),
            "fully_labeled_frozen_cohort" if cohort_complete else "not_available",
        ),
        "supported_time_precision": _metric(
            sum(
                _annotation_bool(item.annotation, "supported") is True
                for item in parsed_calibration
            )
            / len(parsed_calibration)
            if cohort_complete and parsed_calibration
            else None,
            sum(
                _annotation_bool(item.annotation, "supported") is True
                for item in parsed_calibration
            ),
            len(parsed_calibration),
            "all_parsed_records_in_fully_labeled_frozen_cohort"
            if cohort_complete
            else "not_available",
        ),
        "final_judge_acceptance": _metric(
            sum(
                _annotation_bool(item.annotation, "final_judge_acceptable") is True
                for item in parsed_calibration
            )
            / len(parsed_calibration)
            if cohort_complete and parsed_calibration
            else None,
            sum(
                _annotation_bool(item.annotation, "final_judge_acceptable") is True
                for item in parsed_calibration
            ),
            len(parsed_calibration),
            "all_parsed_records_in_fully_labeled_frozen_cohort"
            if cohort_complete
            else "not_available",
        ),
        "critical_errors": _fully_labeled_error_metric(
            calibration_records, "critical_error", cohort_complete=cohort_complete
        ),
        "privacy_violations": _metric(
            0, None, None, "aggregate_schema_structural_assertion"
        ),
        "cross_occurrence_errors": _fully_labeled_error_metric(
            calibration_records,
            "cross_occurrence_error",
            cohort_complete=cohort_complete,
        ),
        "unintended_writes": _metric(
            0, None, None, "read_only_harness_structural_assertion"
        ),
        "nondeterministic_revisions": _metric(
            sum(item.nondeterministic for item in historical),
            sum(item.nondeterministic for item in historical),
            len(historical),
            "exact_double_replay",
        ),
    }
    return metrics


def _evaluate_gates(
    metrics: Mapping[str, Any], gates: HistoricalGmailGateConfig
) -> dict[str, Any]:
    checks = [
        _minimum_gate(
            "historical_coverage",
            metrics["historical_coverage"],
            gates.minimum_historical_coverage,
        ),
        _minimum_gate(
            "calibration_sample_size",
            metrics["calibration_sample_size"],
            gates.minimum_labeled_sample_size,
        ),
        _minimum_gate(
            "calibration_annotation_coverage",
            metrics["calibration_annotation_coverage"],
            1.0,
        ),
        _minimum_gate(
            "calibration_minimum_stratum_count",
            metrics["calibration_minimum_stratum_count"],
            gates.minimum_labeled_per_required_stratum,
        ),
        _minimum_gate(
            "human_temporal_recall",
            metrics["human_temporal_recall"],
            gates.minimum_human_temporal_recall,
        ),
        _minimum_gate(
            "supported_time_precision",
            metrics["supported_time_precision"],
            gates.minimum_supported_time_precision,
        ),
        _minimum_gate(
            "explicit_date_recall_proxy",
            metrics["explicit_date_recall"],
            gates.minimum_explicit_date_recall_proxy,
        ),
        _minimum_gate(
            "current_important_temporal_detection_proxy",
            metrics["current_important_temporal_detection"],
            gates.minimum_important_temporal_detection_proxy,
        ),
        _maximum_gate(
            "fact_eligible_rate_delta",
            metrics["fact_eligible_rate_delta"],
            gates.maximum_fact_eligible_rate_delta,
        ),
        _minimum_gate(
            "final_judge_acceptance",
            metrics["final_judge_acceptance"],
            gates.minimum_final_judge_acceptance,
        ),
    ]
    for name, required in (
        ("critical_errors", gates.require_zero_critical_errors),
        ("privacy_violations", gates.require_zero_privacy_violations),
        ("cross_occurrence_errors", gates.require_zero_cross_occurrence_errors),
        ("unintended_writes", gates.require_zero_unintended_writes),
        ("nondeterministic_revisions", gates.require_deterministic_replay),
    ):
        if required:
            metric = metrics[name]
            value = metric.get("value")
            checks.append(
                {
                    "name": name,
                    "status": (
                        "not_evaluated"
                        if value is None
                        else "pass"
                        if int(value) == 0
                        else "fail"
                    ),
                    "actual": value,
                    "threshold": 0,
                    "basis": metric.get("basis"),
                }
            )
    return {
        "promotion_ready": bool(checks)
        and all(item["status"] == "pass" for item in checks),
        "checks": checks,
    }


def _minimum_gate(
    name: str, metric: Mapping[str, Any], threshold: float
) -> dict[str, Any]:
    value = metric.get("value")
    return {
        "name": name,
        "status": "not_evaluated"
        if value is None
        else "pass"
        if value >= threshold
        else "fail",
        "actual": value,
        "threshold": threshold,
        "basis": metric.get("basis"),
    }


def _maximum_gate(
    name: str, metric: Mapping[str, Any], threshold: float
) -> dict[str, Any]:
    value = metric.get("value")
    return {
        "name": name,
        "status": "not_evaluated"
        if value is None
        else "pass"
        if value <= threshold
        else "fail",
        "actual": value,
        "threshold": threshold,
        "basis": metric.get("basis"),
    }


def _metric(
    value: float | None, numerator: int | None, denominator: int | None, basis: str
) -> dict[str, Any]:
    return {
        "value": value,
        "numerator": numerator,
        "denominator": denominator,
        "basis": basis,
    }


def _fully_labeled_error_metric(
    records: Sequence[_EvaluatedRevision],
    name: str,
    *,
    cohort_complete: bool,
) -> dict[str, Any]:
    targets = [item for item in records if item.parsed]
    labeled = [
        item for item in targets if _annotation_bool(item.annotation, name) is not None
    ]
    fully_labeled = cohort_complete and bool(targets) and len(labeled) == len(targets)
    observed = sum(_annotation_bool(item.annotation, name) is True for item in labeled)
    return _metric(
        observed if fully_labeled else None,
        observed,
        len(targets),
        "fully_labeled_candidate_set" if fully_labeled else "not_available",
    )


def _annotation_bool(annotation: Mapping[str, Any], name: str) -> bool | None:
    value = annotation.get(name)
    return value if isinstance(value, bool) else None


def _noise_class(record: _EvaluatedRevision) -> str:
    if record.fact_importance == "advertising" or record.delivery_kind == "bulk":
        return "advertising_or_bulk"
    if record.fact_importance == "routine":
        return "routine"
    if record.fact_eligible:
        return "fact_eligible_signal"
    return "other_suppressed"


def _default_detector(**kwargs: Any) -> Sequence[Any]:
    from .gmail_temporal_discovery import discover_gmail_temporal_candidates

    return discover_gmail_temporal_candidates(**kwargs)


def _sample_secret(value: bytes | str) -> bytes:
    secret = value.encode("utf-8") if isinstance(value, str) else value
    if not isinstance(secret, bytes) or len(secret) < 16:
        raise HistoricalGmailEvaluationError(
            "sample_secret must contain at least 16 bytes"
        )
    return secret


def _sample_id(secret: bytes, revision_key: str) -> str:
    digest = hmac.new(
        secret,
        b"pkm-brain/gmail-temporal-eval/v1\0" + revision_key.encode("ascii"),
        hashlib.sha256,
    ).hexdigest()
    return f"gte_{digest[:24]}"


def _internal_digest(namespace: str, *values: str) -> str:
    payload = json.dumps(values, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(f"{namespace}\0{payload}".encode("utf-8")).hexdigest()


def _dimension(value: Any, allowed: set[str]) -> str:
    normalized = str(value or "").strip().casefold().replace("-", "_")
    return normalized if normalized in allowed else "unknown"


def _resolution_basis(value: Any) -> str:
    normalized = str(value or "").strip().casefold().replace("-", "_")
    return normalized if normalized in _SAFE_BASES else "other"


def _private_text(value: Any) -> str:
    return str(value or "").strip()


def _strict_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _projection_version(frontmatter: Mapping[str, Any], name: str) -> int:
    raw_value = frontmatter.get(name)
    if name not in frontmatter or raw_value is None or raw_value == "":
        # The original Gmail renderer/classifier predated explicit version fields.
        return 1
    value = _strict_int(raw_value)
    if value is None or value < 1:
        raise HistoricalGmailEvaluationError("Gmail projection version is invalid")
    return value


def _timestamp_rank(value: Any) -> tuple[int, float, str]:
    raw = str(value or "").strip()
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return (0, float("-inf"), raw)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return (1, parsed.timestamp(), raw)


def _truthy(value: Any) -> bool:
    return value is True or str(value or "").strip().casefold() in {"1", "true", "yes"}


def _assert_aggregate_report(report: Mapping[str, Any]) -> None:
    serialized = json.dumps(report, ensure_ascii=True, sort_keys=True)
    forbidden_keys = {
        "title",
        "body",
        "text",
        "address",
        "email",
        "provider_id",
        "thread_id",
        "message_id",
        "account_key",
        "path",
    }

    def visit(value: Any) -> None:
        if isinstance(value, Mapping):
            for key, child in value.items():
                normalized = str(key).casefold()
                if normalized in forbidden_keys:
                    raise HistoricalGmailEvaluationError(
                        "aggregate report contains a forbidden content field"
                    )
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(report)
    if re.search(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", serialized, re.IGNORECASE):
        raise HistoricalGmailEvaluationError("aggregate report contains an address")
    if re.search(r"(?<![a-z0-9])(?:https?://|file://)", serialized, re.IGNORECASE):
        raise HistoricalGmailEvaluationError(
            "aggregate report contains a source locator"
        )


__all__ = [
    "GMAIL_TEMPORAL_EVALUATION_SCHEMA_VERSION",
    "HistoricalGmailEvaluationError",
    "HistoricalGmailGateConfig",
    "evaluate_historical_gmail_projection",
]
