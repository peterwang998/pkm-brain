from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal

from .gmail_temporal_thread_retrieval_experiment import (
    GmailTemporalThreadEvidence,
    GmailTemporalVerifiedEventBinding,
    TemporalThreadIntentOverride,
)


PUBLIC_TEMPORAL_THREAD_RETRIEVAL_FIXTURE_VERSION = (
    "gmail_temporal_thread_retrieval_public_fixture_v2"
)

CaseStratum = Literal[
    "long_current_status",
    "shared_project_multi_event",
    "assertion_scope",
    "pronoun_collision",
    "unique_pronoun_review",
    "acronym",
    "unicode_identity",
    "same_subject_multi_topic",
    "canonical_paraphrase",
    "tail_retention",
    "as_of_exclusion",
    "identity_binding_adversary",
    "control",
]


@dataclass(frozen=True)
class PublicTemporalThreadQuery:
    query_id: str
    query_text: str
    authored_intent: TemporalThreadIntentOverride
    required_evidence_ids: tuple[str, ...]
    review_only_evidence_ids: tuple[str, ...] = ()
    forbidden_context_evidence_ids: tuple[str, ...] = ()
    protected_direct_evidence_ids: tuple[str, ...] = ()
    current_head_evidence_id: str | None = None


@dataclass(frozen=True)
class PublicTemporalThreadRetrievalCase:
    case_id: str
    stratum: CaseStratum
    source_available_as_of: str
    sources: tuple[GmailTemporalThreadEvidence, ...]
    baseline_ranked_evidence_ids: tuple[str, ...]
    queries: tuple[PublicTemporalThreadQuery, ...]
    excluded_evidence_ids: tuple[str, ...] = ()


_BASE = datetime(2027, 10, 1, 8, 0, tzinfo=timezone.utc)

_VERIFIED_EVENT_ALIASES: dict[str, tuple[str, ...]] = {
    "event:apollo-demo": ("Apollo demo",),
    "event:apollo-filing": ("Apollo filing",),
    "event:beacon-summit": ("Beacon summit",),
    "event:benefits-enrollment": ("Benefits enrollment",),
    "event:cedar-audit": ("Cedar audit",),
    "event:cedar-demo": ("Cedar demo",),
    "event:controls-atlas": ("Atlas launch",),
    "event:harbor-summit": ("Harbor summit",),
    "event:harbor-team-lunch": ("Team lunch",),
    "event:iris-offsite": ("Iris offsite",),
    "event:juniper-renewal": ("Juniper renewal", "Juniper"),
    "event:kestrel-rollout": ("Kestrel rollout",),
    "event:lumen-launch": ("Lumen launch",),
    "event:maple-access": ("Maple access",),
    "event:mercury-briefing": ("Mercury briefing",),
    "event:mixed-atlas": ("Atlas launch",),
    "event:mixed-orion": ("Orion review",),
    "event:payroll-cutoff": ("Payroll response",),
    "event:pulsar-workshop": ("Pulsar workshop",),
    "event:q3-business-review": ("Q3 BR", "Q3 Business Review"),
    "event:security-training": ("Security training",),
    "event:tokyo-meeting": ("東京会議",),
    "event:vega-interview": ("Vega interview",),
}


def _source(
    evidence_id: str,
    thread_id: str,
    ordinal: int,
    text: str,
    *,
    hours: int | None = None,
    verified_key: str | None = None,
    contextual_key: str | None = None,
) -> GmailTemporalThreadEvidence:
    verified_bindings = ()
    if verified_key is not None:
        verified_bindings = (
            GmailTemporalVerifiedEventBinding(
                event_identity_key=verified_key,
                aliases=_VERIFIED_EVENT_ALIASES[verified_key],
            ),
        )
    return GmailTemporalThreadEvidence(
        evidence_id=evidence_id,
        gmail_account_scope_id="public-fixture-account",
        gmail_provider_thread_id=thread_id,
        available_at=(
            _BASE + timedelta(hours=hours if hours is not None else ordinal)
        ).isoformat(),
        message_ordinal=ordinal,
        text=text,
        verified_event_bindings=verified_bindings,
        contextual_event_identity_keys=((contextual_key,) if contextual_key else ()),
    )


def _distractors(
    case: str,
    *,
    count: int = 12,
    hour_start: int = 100,
) -> tuple[GmailTemporalThreadEvidence, ...]:
    return tuple(
        _source(
            f"{case}-d{index}",
            f"{case}-distractor-{index}",
            1,
            f"Subject: Routine note {index}\n\nGeneral information only.",
            hours=hour_start + index,
        )
        for index in range(1, count + 1)
    )


def _rank(case: str, *special: tuple[int, str]) -> tuple[str, ...]:
    output = [f"{case}-d{index}" for index in range(1, 11)]
    for position, evidence_id in special:
        output[position - 1] = evidence_id
    return tuple(output)


def _harbor_long_status() -> PublicTemporalThreadRetrievalCase:
    case = "harbor"
    key = "event:harbor-summit"
    thread = (
        _source(
            "harbor-m1",
            "harbor-thread",
            1,
            "Subject: Harbor summit\n\nHarbor summit was booked for October 3, 2027.",
            verified_key=key,
        ),
        _source(
            "harbor-m2",
            "harbor-thread",
            4,
            "Subject: Harbor summit\n\nHarbor summit was rescheduled to October 8, 2027.",
            verified_key=key,
        ),
        _source(
            "harbor-m3",
            "harbor-thread",
            9,
            "Subject: Harbor summit\n\nHarbor summit was cancelled.",
            verified_key=key,
        ),
        _source(
            "harbor-m4",
            "harbor-thread",
            14,
            "Subject: Harbor summit\n\nHarbor summit was confirmed for October 8, 2027.",
            verified_key=key,
        ),
        _source(
            "harbor-m5",
            "harbor-thread",
            20,
            "Subject: Harbor summit\n\nHarbor summit was completed.",
            verified_key=key,
        ),
        _source(
            "harbor-question",
            "harbor-thread",
            21,
            "Subject: Harbor summit\n\nWas Harbor summit completed?",
            verified_key=key,
        ),
        _source(
            "harbor-lunch",
            "harbor-thread",
            22,
            "Subject: Harbor summit\n\nTeam lunch was scheduled for October 9, 2027.",
            verified_key="event:harbor-team-lunch",
        ),
        _source(
            "harbor-reported",
            "harbor-thread",
            23,
            "Subject: Harbor summit\n\nPat said Harbor summit was cancelled.",
            verified_key=key,
        ),
    )
    queries = tuple(
        PublicTemporalThreadQuery(
            query_id=f"harbor-{index}",
            query_text=query,
            authored_intent="lifecycle",
            required_evidence_ids=("harbor-m1", "harbor-m5"),
            review_only_evidence_ids=("harbor-m3", "harbor-m4"),
            forbidden_context_evidence_ids=(
                "harbor-question",
                "harbor-lunch",
                "harbor-reported",
            ),
            current_head_evidence_id="harbor-m5",
        )
        for index, query in enumerate(
            (
                "What is the current status of the Harbor summit?",
                "What’s the latest on the Harbor summit?",
                "Did the Harbor summit end up going ahead?",
            ),
            start=1,
        )
    )
    return PublicTemporalThreadRetrievalCase(
        case_id=case,
        stratum="long_current_status",
        source_available_as_of="2027-10-31T00:00:00Z",
        sources=thread + _distractors(case),
        baseline_ranked_evidence_ids=_rank(case, (1, "harbor-m1")),
        queries=queries,
    )


def _apollo_shared_project() -> PublicTemporalThreadRetrievalCase:
    case = "apollo"
    demo = "event:apollo-demo"
    filing = "event:apollo-filing"
    thread = (
        _source(
            "apollo-demo-m1",
            "apollo-thread",
            1,
            "Subject: Apollo project\n\nApollo demo was booked for October 3, 2027.",
            verified_key=demo,
        ),
        _source(
            "apollo-filing-m1",
            "apollo-thread",
            2,
            "Subject: Apollo project\n\nApollo filing is due by October 4, 2027.",
            verified_key=filing,
        ),
        _source(
            "apollo-demo-m2",
            "apollo-thread",
            3,
            "Subject: Apollo project\n\nApollo demo was rescheduled to October 7, 2027.",
            verified_key=demo,
        ),
        _source(
            "apollo-pronoun",
            "apollo-thread",
            4,
            "Subject: Apollo project\n\nIt was cancelled.",
        ),
        _source(
            "apollo-filing-m2",
            "apollo-thread",
            5,
            "Subject: Apollo project\n\nApollo filing was cancelled.",
            verified_key=filing,
        ),
    )
    return PublicTemporalThreadRetrievalCase(
        case_id=case,
        stratum="shared_project_multi_event",
        source_available_as_of="2027-10-31T00:00:00Z",
        sources=thread + _distractors(case),
        baseline_ranked_evidence_ids=_rank(case, (1, "apollo-demo-m1")),
        queries=(
            PublicTemporalThreadQuery(
                query_id="apollo-demo-status",
                query_text="What is the latest status of the Apollo demo?",
                authored_intent="lifecycle",
                required_evidence_ids=("apollo-demo-m1", "apollo-demo-m2"),
                review_only_evidence_ids=("apollo-pronoun",),
                forbidden_context_evidence_ids=(
                    "apollo-filing-m1",
                    "apollo-filing-m2",
                ),
                current_head_evidence_id="apollo-demo-m2",
            ),
            PublicTemporalThreadQuery(
                query_id="apollo-filing-with-demo-anchor",
                query_text="What is the latest status of the Apollo filing?",
                authored_intent="lifecycle",
                required_evidence_ids=("apollo-demo-m1",),
                forbidden_context_evidence_ids=(
                    "apollo-demo-m2",
                    "apollo-filing-m1",
                    "apollo-filing-m2",
                    "apollo-pronoun",
                ),
                protected_direct_evidence_ids=_rank(case, (1, "apollo-demo-m1")),
            ),
        ),
    )


def _mercury_assertion_scope() -> PublicTemporalThreadRetrievalCase:
    case = "mercury"
    key = "event:mercury-briefing"
    messages = (
        ("mercury-anchor", 1, "Mercury briefing was booked for October 3, 2027."),
        ("mercury-conditional", 2, "If Mercury briefing is cancelled, call me."),
        ("mercury-quote", 3, "> Mercury briefing was cancelled."),
        (
            "mercury-original",
            4,
            "-----Original Message-----\nMercury briefing was cancelled.",
        ),
        ("mercury-reported", 5, "They said Mercury briefing was cancelled."),
        (
            "mercury-refuted",
            6,
            "Mercury briefing was cancelled, but that is wrong.",
        ),
        ("mercury-question", 7, "Was Mercury briefing cancelled?"),
        (
            "mercury-reschedule",
            8,
            "Parking was not included. Mercury briefing was rescheduled to October 9, 2027.",
        ),
        (
            "mercury-inline-quote",
            9,
            "The note said “Mercury briefing was cancelled.”",
        ),
        (
            "mercury-epistemic",
            10,
            "Mercury briefing was probably cancelled.",
        ),
        (
            "mercury-forwarded",
            11,
            "Begin forwarded message:\nMercury briefing was completed.",
        ),
    )
    thread = tuple(
        _source(
            evidence_id,
            "mercury-thread",
            ordinal,
            f"Subject: Mercury briefing\n\n{text}",
            verified_key=key,
        )
        for evidence_id, ordinal, text in messages
    )
    forbidden = tuple(
        item[0] for item in messages[1:] if item[0] != "mercury-reschedule"
    )
    return PublicTemporalThreadRetrievalCase(
        case_id=case,
        stratum="assertion_scope",
        source_available_as_of="2027-10-31T00:00:00Z",
        sources=thread + _distractors(case),
        baseline_ranked_evidence_ids=_rank(case, (1, "mercury-anchor")),
        queries=(
            PublicTemporalThreadQuery(
                query_id="mercury-timeline",
                query_text="Walk me through the Mercury briefing schedule changes.",
                authored_intent="timeline",
                required_evidence_ids=("mercury-anchor", "mercury-reschedule"),
                forbidden_context_evidence_ids=forbidden,
            ),
        ),
    )


def _cedar_pronoun_collision() -> PublicTemporalThreadRetrievalCase:
    case = "cedar"
    demo = "event:cedar-demo"
    audit = "event:cedar-audit"
    thread = (
        _source(
            "cedar-demo-m1",
            "cedar-thread",
            1,
            "Subject: Cedar program\n\nCedar demo was booked for October 3, 2027.",
            verified_key=demo,
        ),
        _source(
            "cedar-audit-m1",
            "cedar-thread",
            2,
            "Subject: Cedar program\n\nCedar audit was booked for October 4, 2027.",
            verified_key=audit,
        ),
        _source(
            "cedar-pronoun",
            "cedar-thread",
            3,
            "Subject: Cedar program\n\nIt was rescheduled to October 6, 2027.",
        ),
        _source(
            "cedar-demo-m2",
            "cedar-thread",
            4,
            "Subject: Cedar program\n\nCedar demo was confirmed for October 6, 2027.",
            verified_key=demo,
        ),
    )
    return PublicTemporalThreadRetrievalCase(
        case_id=case,
        stratum="pronoun_collision",
        source_available_as_of="2027-10-31T00:00:00Z",
        sources=thread + _distractors(case),
        baseline_ranked_evidence_ids=_rank(case, (1, "cedar-demo-m1")),
        queries=(
            PublicTemporalThreadQuery(
                query_id="cedar-demo-timeline",
                query_text="What happened with the Cedar demo schedule?",
                authored_intent="timeline",
                required_evidence_ids=("cedar-demo-m1", "cedar-demo-m2"),
                review_only_evidence_ids=("cedar-pronoun",),
                forbidden_context_evidence_ids=("cedar-audit-m1",),
            ),
        ),
    )


def _q3_br() -> PublicTemporalThreadRetrievalCase:
    case = "q3br"
    key = "event:q3-business-review"
    thread = (
        _source(
            "q3br-m1",
            "q3br-thread",
            1,
            "Subject: Q3 BR\n\nQ3 BR was booked for October 3, 2027.",
            verified_key=key,
        ),
        _source(
            "q3br-m2",
            "q3br-thread",
            2,
            "Subject: Q3 BR\n\nQ3 BR was confirmed for October 8, 2027.",
            verified_key=key,
        ),
        _source(
            "q3br-m3",
            "q3br-thread",
            3,
            "Subject: Q3 BR\n\nQ3 BR was called off.",
            verified_key=key,
        ),
    )
    queries = (
        PublicTemporalThreadQuery(
            query_id="q3br-status",
            query_text="What is the current status of Q3 BR?",
            authored_intent="lifecycle",
            required_evidence_ids=("q3br-m1", "q3br-m3"),
            review_only_evidence_ids=("q3br-m2",),
            current_head_evidence_id="q3br-m3",
        ),
        PublicTemporalThreadQuery(
            query_id="q3br-history",
            query_text="What happened with Q3 BR?",
            authored_intent="timeline",
            required_evidence_ids=("q3br-m1", "q3br-m2", "q3br-m3"),
        ),
        PublicTemporalThreadQuery(
            query_id="q3br-land",
            query_text="Where did Q3 BR land?",
            authored_intent="timeline",
            required_evidence_ids=("q3br-m1", "q3br-m2", "q3br-m3"),
        ),
    )
    return PublicTemporalThreadRetrievalCase(
        case_id=case,
        stratum="acronym",
        source_available_as_of="2027-10-31T00:00:00Z",
        sources=thread + _distractors(case),
        baseline_ranked_evidence_ids=_rank(case, (1, "q3br-m1")),
        queries=queries,
    )


def _unique_pronoun_review() -> PublicTemporalThreadRetrievalCase:
    case = "vega"
    key = "event:vega-interview"
    thread = (
        _source(
            "vega-m1",
            "vega-thread",
            1,
            "Subject: Vega interview\n\nVega interview was booked for October 3, 2027.",
            verified_key=key,
        ),
        _source(
            "vega-m2",
            "vega-thread",
            2,
            "Subject: Vega interview\n\nVega interview was rescheduled to October 8, 2027.",
            verified_key=key,
        ),
        _source(
            "vega-pronoun-review",
            "vega-thread",
            3,
            "Subject: Vega interview\n\nIt was cancelled.",
            contextual_key=key,
        ),
    )
    return PublicTemporalThreadRetrievalCase(
        case_id=case,
        stratum="unique_pronoun_review",
        source_available_as_of="2027-10-31T00:00:00Z",
        sources=thread + _distractors(case),
        baseline_ranked_evidence_ids=_rank(case, (1, "vega-m1")),
        queries=(
            PublicTemporalThreadQuery(
                query_id="vega-timeline",
                query_text="What happened with the Vega interview?",
                authored_intent="timeline",
                required_evidence_ids=("vega-m1", "vega-m2"),
                review_only_evidence_ids=("vega-pronoun-review",),
            ),
        ),
    )


def _unicode_identity() -> PublicTemporalThreadRetrievalCase:
    case = "tokyo"
    key = "event:tokyo-meeting"
    thread = (
        _source(
            "tokyo-m1",
            "tokyo-thread",
            1,
            "Subject: 東京会議\n\n東京会議 was booked for October 3, 2027.",
            verified_key=key,
        ),
        _source(
            "tokyo-m2",
            "tokyo-thread",
            2,
            "Subject: 東京会議\n\n東京会議 was confirmed for October 6, 2027.",
            verified_key=key,
        ),
        _source(
            "tokyo-m3",
            "tokyo-thread",
            3,
            "Subject: 東京会議\n\n東京会議 was called off.",
            verified_key=key,
        ),
    )
    return PublicTemporalThreadRetrievalCase(
        case_id=case,
        stratum="unicode_identity",
        source_available_as_of="2027-10-31T00:00:00Z",
        sources=thread + _distractors(case),
        baseline_ranked_evidence_ids=_rank(case, (1, "tokyo-m1")),
        queries=(
            PublicTemporalThreadQuery(
                query_id="tokyo-status",
                query_text="What’s the latest on 東京会議?",
                authored_intent="lifecycle",
                required_evidence_ids=("tokyo-m1", "tokyo-m3"),
                review_only_evidence_ids=("tokyo-m2",),
                current_head_evidence_id="tokyo-m3",
            ),
            PublicTemporalThreadQuery(
                query_id="tokyo-timeline",
                query_text="東京会議 timeline",
                authored_intent="timeline",
                required_evidence_ids=("tokyo-m1", "tokyo-m2", "tokyo-m3"),
            ),
        ),
    )


def _weekly_ops_multitopic() -> PublicTemporalThreadRetrievalCase:
    case = "weekly"
    kestrel = "event:kestrel-rollout"
    thread = (
        _source(
            "weekly-kestrel-m1",
            "weekly-thread",
            1,
            "Subject: Weekly Ops\n\nKestrel rollout was booked for October 3, 2027.",
            verified_key=kestrel,
        ),
        _source(
            "weekly-payroll",
            "weekly-thread",
            2,
            "Subject: Weekly Ops\n\nPayroll response is due by October 4, 2027.",
            verified_key="event:payroll-cutoff",
        ),
        _source(
            "weekly-security",
            "weekly-thread",
            3,
            "Subject: Weekly Ops\n\nSecurity training was scheduled for October 5, 2027.",
            verified_key="event:security-training",
        ),
        _source(
            "weekly-kestrel-m2",
            "weekly-thread",
            4,
            "Subject: Weekly Ops\n\nKestrel rollout was rescheduled to October 9, 2027.",
            verified_key=kestrel,
        ),
        _source(
            "weekly-benefits",
            "weekly-thread",
            5,
            "Subject: Weekly Ops\n\nBenefits enrollment expires October 7, 2027.",
            verified_key="event:benefits-enrollment",
        ),
    )
    return PublicTemporalThreadRetrievalCase(
        case_id=case,
        stratum="same_subject_multi_topic",
        source_available_as_of="2027-10-31T00:00:00Z",
        sources=thread + _distractors(case),
        baseline_ranked_evidence_ids=_rank(case, (1, "weekly-kestrel-m1")),
        queries=(
            PublicTemporalThreadQuery(
                query_id="weekly-kestrel",
                query_text="How did the Kestrel rollout schedule change?",
                authored_intent="timeline",
                required_evidence_ids=("weekly-kestrel-m1", "weekly-kestrel-m2"),
                forbidden_context_evidence_ids=(
                    "weekly-payroll",
                    "weekly-security",
                    "weekly-benefits",
                ),
            ),
        ),
    )


def _canonical_paraphrase_case(
    *,
    case: str,
    key: str,
    anchor_text: str,
    evidence_text: str,
    query: str,
) -> PublicTemporalThreadRetrievalCase:
    anchor_id = f"{case}-anchor"
    evidence_id = f"{case}-evidence"
    thread = (
        _source(
            anchor_id,
            f"{case}-thread",
            1,
            f"Subject: {case}\n\n{anchor_text}",
            verified_key=key,
        ),
        _source(
            evidence_id,
            f"{case}-thread",
            2,
            f"Subject: {case}\n\n{evidence_text}",
            verified_key=key,
        ),
    )
    return PublicTemporalThreadRetrievalCase(
        case_id=case,
        stratum="canonical_paraphrase",
        source_available_as_of="2027-10-31T00:00:00Z",
        sources=thread + _distractors(case),
        baseline_ranked_evidence_ids=_rank(case, (1, anchor_id)),
        queries=(
            PublicTemporalThreadQuery(
                query_id=f"{case}-query",
                query_text=query,
                authored_intent="timeline",
                required_evidence_ids=(anchor_id, evidence_id),
            ),
        ),
    )


def _beacon_tail_retention() -> PublicTemporalThreadRetrievalCase:
    case = "beacon"
    key = "event:beacon-summit"
    thread = (
        _source(
            "beacon-m1",
            "beacon-thread",
            1,
            "Subject: Beacon summit\n\nBeacon summit was booked for October 3, 2027.",
            verified_key=key,
        ),
        _source(
            "beacon-m2",
            "beacon-thread",
            2,
            "Subject: Beacon summit\n\nBeacon summit was rescheduled to October 7, 2027.",
            verified_key=key,
        ),
        _source(
            "beacon-m3",
            "beacon-thread",
            3,
            "Subject: Beacon summit\n\nBeacon summit was confirmed for October 7, 2027.",
            verified_key=key,
        ),
        _source(
            "beacon-m4",
            "beacon-thread",
            4,
            "Subject: Beacon summit\n\nBeacon summit was completed.",
            verified_key=key,
        ),
        _source(
            "beacon-approval",
            "beacon-approval-thread",
            1,
            "Subject: Re: approval\n\nYes—the organizer and legal both approved it.",
        ),
    )
    return PublicTemporalThreadRetrievalCase(
        case_id=case,
        stratum="tail_retention",
        source_available_as_of="2027-10-31T00:00:00Z",
        sources=thread + _distractors(case),
        baseline_ranked_evidence_ids=_rank(
            case,
            (1, "beacon-m1"),
            (10, "beacon-approval"),
        ),
        queries=(
            PublicTemporalThreadQuery(
                query_id="beacon-timeline",
                query_text="Walk me through the Beacon summit schedule.",
                authored_intent="timeline",
                required_evidence_ids=(
                    "beacon-m1",
                    "beacon-m2",
                    "beacon-m3",
                    "beacon-m4",
                ),
                protected_direct_evidence_ids=("beacon-approval",),
            ),
        ),
    )


def _as_of_exclusion() -> PublicTemporalThreadRetrievalCase:
    case = "pulsar"
    key = "event:pulsar-workshop"
    thread = (
        _source(
            "pulsar-m1",
            "pulsar-thread",
            1,
            "Subject: Pulsar workshop\n\nPulsar workshop was booked for October 2, 2027.",
            hours=1,
            verified_key=key,
        ),
        _source(
            "pulsar-m2",
            "pulsar-thread",
            2,
            "Subject: Pulsar workshop\n\nPulsar workshop was rescheduled to October 4, 2027.",
            hours=20,
            verified_key=key,
        ),
        _source(
            "pulsar-excluded",
            "pulsar-thread",
            3,
            "Subject: Pulsar workshop\n\nPulsar workshop was confirmed for October 4, 2027.",
            hours=25,
            verified_key=key,
        ),
        _source(
            "pulsar-future",
            "pulsar-thread",
            4,
            "Subject: Pulsar workshop\n\nPulsar workshop was cancelled.",
            hours=80,
            verified_key=key,
        ),
    )
    distractors = _distractors(case, hour_start=30)
    return PublicTemporalThreadRetrievalCase(
        case_id=case,
        stratum="as_of_exclusion",
        source_available_as_of="2027-10-03T23:59:59Z",
        sources=thread + distractors,
        baseline_ranked_evidence_ids=_rank(case, (1, "pulsar-m1")),
        excluded_evidence_ids=("pulsar-excluded",),
        queries=(
            PublicTemporalThreadQuery(
                query_id="pulsar-as-of",
                query_text="As of October 3, what was the Pulsar workshop status?",
                authored_intent="lifecycle",
                required_evidence_ids=("pulsar-m1", "pulsar-m2"),
                forbidden_context_evidence_ids=(
                    "pulsar-excluded",
                    "pulsar-future",
                ),
                current_head_evidence_id="pulsar-m2",
            ),
        ),
    )


def _mixed_content_wrong_key() -> PublicTemporalThreadRetrievalCase:
    case = "mixed"
    thread = (
        _source(
            "mixed-orion-anchor",
            "mixed-thread",
            1,
            (
                "Subject: Orion review and Atlas launch\n\n"
                "Atlas launch dependencies are covered. "
                "Orion review was booked for October 3, 2027."
            ),
            verified_key="event:mixed-orion",
        ),
        _source(
            "mixed-atlas-update",
            "mixed-thread",
            2,
            "Subject: Atlas launch\n\nAtlas launch was cancelled.",
            verified_key="event:mixed-atlas",
        ),
    )
    baseline = _rank(case, (1, "mixed-orion-anchor"))
    return PublicTemporalThreadRetrievalCase(
        case_id=case,
        stratum="identity_binding_adversary",
        source_available_as_of="2027-10-31T00:00:00Z",
        sources=thread + _distractors(case),
        baseline_ranked_evidence_ids=baseline,
        queries=(
            PublicTemporalThreadQuery(
                query_id="mixed-atlas-wrong-key-anchor",
                query_text="What is the current status of the Atlas launch?",
                authored_intent="lifecycle",
                required_evidence_ids=("mixed-orion-anchor",),
                forbidden_context_evidence_ids=("mixed-atlas-update",),
                protected_direct_evidence_ids=baseline,
            ),
        ),
    )


def _controls() -> PublicTemporalThreadRetrievalCase:
    case = "controls"
    hidden = (
        _source(
            "controls-atlas-m1",
            "controls-atlas-thread",
            1,
            "Subject: Atlas launch\n\nAtlas launch was booked for October 3, 2027.",
            verified_key="event:controls-atlas",
        ),
        _source(
            "controls-atlas-m2",
            "controls-atlas-thread",
            2,
            "Subject: Atlas launch\n\nAtlas launch was cancelled.",
            verified_key="event:controls-atlas",
        ),
    )
    sources = hidden + _distractors(case)
    baseline = tuple(f"controls-d{i}" for i in range(1, 11))
    return PublicTemporalThreadRetrievalCase(
        case_id=case,
        stratum="control",
        source_available_as_of="2027-10-31T00:00:00Z",
        sources=sources,
        baseline_ranked_evidence_ids=baseline,
        queries=(
            PublicTemporalThreadQuery(
                query_id="control-summary",
                query_text="Summarize the planning discussion.",
                authored_intent="none",
                required_evidence_ids=("controls-d1",),
                protected_direct_evidence_ids=baseline,
            ),
            PublicTemporalThreadQuery(
                query_id="control-rome-history",
                query_text="What is the history of Rome?",
                authored_intent="none",
                required_evidence_ids=("controls-d1",),
                protected_direct_evidence_ids=baseline,
            ),
            PublicTemporalThreadQuery(
                query_id="control-no-anchor",
                query_text="What is the current status of the Atlas launch?",
                authored_intent="lifecycle",
                required_evidence_ids=("controls-d1",),
                forbidden_context_evidence_ids=(
                    "controls-atlas-m1",
                    "controls-atlas-m2",
                ),
                protected_direct_evidence_ids=baseline,
            ),
        ),
    )


PUBLIC_TEMPORAL_THREAD_RETRIEVAL_CASES = (
    _harbor_long_status(),
    _apollo_shared_project(),
    _mercury_assertion_scope(),
    _cedar_pronoun_collision(),
    _unique_pronoun_review(),
    _q3_br(),
    _unicode_identity(),
    _weekly_ops_multitopic(),
    _canonical_paraphrase_case(
        case="iris",
        key="event:iris-offsite",
        anchor_text="Iris offsite planning is active.",
        evidence_text="Iris offsite was booked for October 3, 2027 and confirmed for October 6, 2027.",
        query="When did we land on for the Iris offsite?",
    ),
    _canonical_paraphrase_case(
        case="juniper",
        key="event:juniper-renewal",
        anchor_text="Juniper renewal needs an answer.",
        evidence_text="Please respond by October 9, 2027 regarding Juniper renewal.",
        query="By when do I need to answer Juniper?",
    ),
    _canonical_paraphrase_case(
        case="maple",
        key="event:maple-access",
        anchor_text="Maple access is active.",
        evidence_text="Maple access expires October 10, 2027.",
        query="When does Maple access run out?",
    ),
    _canonical_paraphrase_case(
        case="lumen",
        key="event:lumen-launch",
        anchor_text="Lumen launch planning is active.",
        evidence_text="October 12, 2027 is now the launch date for Lumen.",
        query="What date did we settle on for the Lumen launch?",
    ),
    _beacon_tail_retention(),
    _as_of_exclusion(),
    _mixed_content_wrong_key(),
    _controls(),
)
