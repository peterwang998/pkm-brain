from __future__ import annotations

import argparse
import hashlib
import json
import os
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from pkm_brain.gmail_temporal_leads import analyze_gmail_temporal_leads


DEFAULT_INTERNAL_AT = "2027-08-10T09:00:00-07:00"


@dataclass(frozen=True)
class SyntheticCase:
    sample_id: str
    stratum: str
    text: str
    expected_material: bool
    expected_filter: str
    risk_bucket: str
    message_internal_at: str = DEFAULT_INTERNAL_AT


CASES: tuple[SyntheticCase, ...] = (
    SyntheticCase(
        "syn_clear_01",
        "important_high_confidence",
        "Subject: Nimbus Interview\n\nNimbus Interview is scheduled for "
        "August 14, 2027 at 16:30 -07:00.",
        True,
        "should_admit",
        "clear_planned_occurrence",
    ),
    SyntheticCase(
        "syn_clear_02",
        "important_high_confidence",
        "The dental appointment is scheduled for September 2, 2027.",
        True,
        "should_admit",
        "clear_planned_occurrence",
    ),
    SyntheticCase(
        "syn_clear_03",
        "important_high_confidence",
        "Please submit the fellowship application by August 20, 2027.",
        True,
        "should_admit",
        "clear_deadline",
    ),
    SyntheticCase(
        "syn_clear_04",
        "important_high_confidence",
        "The Atlas planning meeting was held on August 11, 2027.",
        True,
        "should_admit",
        "clear_actual_occurrence",
    ),
    SyntheticCase(
        "syn_clear_05",
        "important_high_confidence",
        "Subject: Cedar Leadership Forum\n\nWhen: August 18, 2027",
        True,
        "should_admit",
        "subject_body_bridge",
    ),
    SyntheticCase(
        "syn_clear_06",
        "important_high_confidence",
        "The project review is tomorrow morning.",
        True,
        "should_admit",
        "relative_coarse_time",
    ),
    SyntheticCase(
        "syn_clear_07",
        "important_high_confidence",
        "Alpha meeting is scheduled for August 14, 2027. "
        "Beta workshop is scheduled for August 16, 2027.",
        True,
        "should_admit",
        "multiple_local_events",
    ),
    SyntheticCase(
        "syn_clear_08",
        "important_high_confidence",
        "By August 22, 2027, send the board packet.",
        True,
        "should_admit",
        "clear_deadline_reversed_order",
    ),
    SyntheticCase(
        "syn_lifecycle_01",
        "important_lifecycle",
        "The dentist appointment scheduled for August 14, 2027 was cancelled.",
        True,
        "should_admit",
        "cancelled_event",
    ),
    SyntheticCase(
        "syn_lifecycle_02",
        "important_lifecycle",
        "The planning workshop on August 12, 2027 was completed.",
        True,
        "should_admit",
        "completed_event",
    ),
    SyntheticCase(
        "syn_lifecycle_03",
        "important_lifecycle",
        "The hiring interview was rescheduled from August 14, 2027 to "
        "August 16, 2027.",
        True,
        "should_admit",
        "rescheduled_endpoints",
    ),
    SyntheticCase(
        "syn_lifecycle_04",
        "important_lifecycle",
        "The application deadline was extended to August 25, 2027.",
        True,
        "should_admit",
        "deadline_extension",
    ),
    SyntheticCase(
        "syn_lifecycle_05",
        "important_lifecycle",
        "Alpha meeting was cancelled August 14, 2027. "
        "Beta meeting is scheduled for August 15, 2027.",
        True,
        "should_admit",
        "lifecycle_scope_isolation",
    ),
    SyntheticCase(
        "syn_lifecycle_06",
        "important_lifecycle",
        "The review meeting took place on August 9, 2027 and was completed "
        "that afternoon.",
        True,
        "should_admit",
        "actual_plus_completion",
    ),
    SyntheticCase(
        "syn_ambiguous_01",
        "important_ambiguous",
        "The strategy meeting is scheduled for 7/8/2027.",
        True,
        "should_admit",
        "locale_ambiguous_date",
    ),
    SyntheticCase(
        "syn_ambiguous_02",
        "important_ambiguous",
        "The investor call is scheduled for August 14, 2027 at 4:30 PM PDT.",
        True,
        "should_admit",
        "timezone_abbreviation",
    ),
    SyntheticCase(
        "syn_ambiguous_03",
        "important_ambiguous",
        "The design review is Friday.",
        True,
        "should_admit",
        "weekday_convention",
    ),
    SyntheticCase(
        "syn_ambiguous_04",
        "important_ambiguous",
        "Subject: Juniper Interview\n\nPossible dates are August 18, 2027 or "
        "August 19, 2027.",
        True,
        "should_admit",
        "multiple_date_options",
    ),
    SyntheticCase(
        "syn_ambiguous_05",
        "important_ambiguous",
        "The product summit runs August 14-16, 2027.",
        True,
        "should_admit",
        "occurrence_range",
    ),
    SyntheticCase(
        "syn_ambiguous_06",
        "important_ambiguous",
        "The Northwind flight arrives August 14, 2027 at 6:10 PM.",
        True,
        "should_admit",
        "terminal_boundary",
    ),
    SyntheticCase(
        "syn_ambiguous_07",
        "important_ambiguous",
        "Please send the revised report before lunch tomorrow.",
        True,
        "should_admit",
        "coarse_relative_deadline",
    ),
    SyntheticCase(
        "syn_ambiguous_08",
        "important_ambiguous",
        "The research sync happens every Tuesday.",
        True,
        "should_admit",
        "recurring_occurrence",
    ),
    SyntheticCase(
        "syn_hard_01",
        "durable_lead",
        "The new benefits policy becomes effective August 1, 2027.",
        True,
        "should_admit",
        "effective_state_change",
    ),
    SyntheticCase(
        "syn_hard_02",
        "durable_lead",
        "Registration opens August 12, 2027 and closes August 20, 2027.",
        True,
        "should_admit",
        "opening_and_closing_boundaries",
    ),
    SyntheticCase(
        "syn_noise_01",
        "suppressed_advertising_temporal",
        "Join our free product webinar on August 14, 2027 and discover ten "
        "ways to grow revenue. Unsubscribe anytime.",
        False,
        "should_suppress",
        "promotional_event",
    ),
    SyntheticCase(
        "syn_noise_02",
        "suppressed_advertising_temporal",
        "Flash sale ends August 16, 2027. Save 40 percent on all accessories.",
        False,
        "should_suppress",
        "promotional_deadline",
    ),
    SyntheticCase(
        "syn_noise_03",
        "suppressed_routine_temporal",
        "Your package is expected to arrive August 14, 2027. Tracking number "
        "SYNTHETIC-123.",
        False,
        "should_suppress",
        "routine_delivery_boundary",
    ),
    SyntheticCase(
        "syn_noise_04",
        "suppressed_routine_temporal",
        "Invoice 10042 was issued on August 10, 2027. No action is required.",
        False,
        "should_suppress",
        "transaction_metadata",
    ),
    SyntheticCase(
        "syn_noise_05",
        "suppressed_advertising_temporal",
        "Weekly Growth Digest — published August 10, 2027. Read the latest "
        "industry headlines.",
        False,
        "should_suppress",
        "publication_metadata",
    ),
    SyntheticCase(
        "syn_noise_06",
        "suppressed_routine_temporal",
        "Copyright 2027 Example Systems. This automated footer was updated "
        "August 1, 2027.",
        False,
        "should_suppress",
        "footer_metadata",
    ),
    SyntheticCase(
        "syn_noise_07",
        "suppressed_routine_temporal",
        "Subject: Status update\n\nNo meeting is being scheduled.\n\n> The old "
        "planning meeting was scheduled for August 14, 2027.",
        False,
        "should_suppress",
        "quoted_history",
    ),
    SyntheticCase(
        "syn_noise_08",
        "suppressed_routine_temporal",
        "Your one-time login code expires at 4:30 PM today. If you did not "
        "request it, ignore this message.",
        False,
        "should_suppress",
        "security_expiration",
    ),
    SyntheticCase(
        "syn_noise_09",
        "suppressed_advertising_temporal",
        "The summer movie premiere is August 20, 2027. Buy tickets now.",
        False,
        "should_suppress",
        "entertainment_promotion",
    ),
    SyntheticCase(
        "syn_noise_10",
        "suppressed_routine_temporal",
        "Routine platform maintenance is scheduled for August 13, 2027 at "
        "2:00 AM UTC. No customer action is needed.",
        False,
        "should_suppress",
        "routine_service_notice",
    ),
    SyntheticCase(
        "syn_noise_11",
        "suppressed_routine_temporal",
        "The attached meeting agenda was revised August 10, 2027; the meeting "
        "date has not been decided.",
        False,
        "should_suppress",
        "artifact_revision_metadata",
    ),
    SyntheticCase(
        "syn_noise_12",
        "suppressed_advertising_temporal",
        "Daily forecast for August 14, 2027: sunny, with a high near 75. "
        "Download our weather app.",
        False,
        "should_suppress",
        "forecast_date",
    ),
)


def _without_chunk(value: dict[str, Any]) -> dict[str, Any]:
    output = dict(value)
    output.pop("chunk_id", None)
    return output


def _record(case: SyntheticCase) -> dict[str, Any]:
    admitted = case.stratum.startswith(("important_", "durable_"))
    analysis = analyze_gmail_temporal_leads(
        text=case.text,
        message_internal_at=case.message_internal_at,
        fact_admitted=admitted,
        temporal_review_rescue=not admitted,
        chunk_id=case.sample_id,
    )
    return {
        "sample_id": case.sample_id,
        "stratum": case.stratum,
        "message_internal_at": case.message_internal_at,
        "context_truncated_before": False,
        "context_truncated_after": False,
        "text": case.text,
        "expressions": [_without_chunk(asdict(item)) for item in analysis.expressions],
        "mentions": [_without_chunk(asdict(item)) for item in analysis.mentions],
        "leads": [_without_chunk(asdict(item)) for item in analysis.leads],
        "gold": {
            "expected_material": case.expected_material,
            "expected_filter": case.expected_filter,
            "risk_bucket": case.risk_bucket,
        },
    }


def build(output: Path) -> dict[str, Any]:
    records = [_record(case) for case in CASES]
    payload = (
        "\n".join(
            json.dumps(item, sort_keys=True, separators=(",", ":"))
            for item in records
        )
        + "\n"
    ).encode("utf-8")
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(
        output,
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(payload)
    os.chmod(output, 0o600)

    strata = Counter(item["stratum"] for item in records)
    risks = Counter(item["gold"]["risk_bucket"] for item in records)
    return {
        "records": len(records),
        "material_records": sum(item["gold"]["expected_material"] for item in records),
        "suppressed_records": sum(
            item["gold"]["expected_filter"] == "should_suppress" for item in records
        ),
        "expressions": sum(len(item["expressions"]) for item in records),
        "mentions": sum(len(item["mentions"]) for item in records),
        "leads": sum(len(item["leads"]) for item in records),
        "strata": dict(sorted(strata.items())),
        "risk_buckets": len(risks),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "file_mode": oct(output.stat().st_mode & 0o777),
        "private_content_printed": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    print(json.dumps(build(args.output), sort_keys=True))


if __name__ == "__main__":
    main()
