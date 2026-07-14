from __future__ import annotations

import json

from pkm_brain.gmail_operations import (
    GmailDetectorBudget,
    detect_gmail_threads,
    preclassify_gmail_thread,
)
from pkm_brain.google_normalization import (
    NormalizedGmailMessage,
    NormalizedGmailThread,
)


class FakeProvider:
    name = "fake"
    model = "fake-operations"

    def __init__(self, responses: list[dict[str, object]]) -> None:
        self.responses = list(responses)
        self.calls = 0

    def complete(self, prompt: str) -> str:
        assert "read-only operational email detector" in prompt
        self.calls += 1
        return json.dumps(self.responses.pop(0))


def message(
    message_id: str,
    body: str,
    *,
    outgoing: bool = False,
    sender: str = "person@example.com",
) -> NormalizedGmailMessage:
    return NormalizedGmailMessage(
        message_id=message_id,
        thread_id=f"thread-{message_id.split('-')[0]}",
        internal_date="1783960000000",
        timestamp="2026-07-13T16:26:40+00:00",
        from_addresses=("operator@example.com",) if outgoing else (sender,),
        to_addresses=("person@example.com",) if outgoing else ("operator@example.com",),
        cc_addresses=(),
        subject="Operational request",
        date_header=None,
        internet_message_id=f"<{message_id}@example.com>",
        in_reply_to=None,
        references=(),
        label_ids=("SENT",) if outgoing else ("INBOX",),
        outgoing=outgoing,
        operator_authored=outgoing,
        body=body,
        body_kind="text/plain",
        attachment_count=0,
        quoted_chars_removed=0,
        truncated=False,
    )


def thread(
    thread_id: str,
    subject: str,
    messages: tuple[NormalizedGmailMessage, ...],
    *,
    message_class: str = "human",
) -> NormalizedGmailThread:
    fixed = tuple(
        NormalizedGmailMessage(**{**item.__dict__, "thread_id": thread_id})
        for item in messages
    )
    return NormalizedGmailThread(
        thread_id=thread_id,
        history_id="12345",
        source_revision="12345",
        subject=subject,
        created_at="2026-07-13T16:00:00+00:00",
        updated_at="2026-07-13T16:26:40+00:00",
        message_class=message_class,
        messages=fixed,
        body_chars=sum(len(item.body) for item in fixed),
        attachment_count=0,
        quoted_chars_removed=0,
        truncated=False,
    )


def test_preclassifier_suppresses_marketing_but_keeps_transactional_deadlines() -> None:
    newsletter = thread(
        "newsletter",
        "Weekly product newsletter",
        (message("newsletter-1", "Read our newsletter and unsubscribe anytime."),),
        message_class="bulk",
    )
    renewal = thread(
        "renewal",
        "Your membership renewal is due",
        (message("renewal-1", "Payment is due tomorrow. Please renew."),),
        message_class="transactional",
    )
    newsletter_result = preclassify_gmail_thread(
        newsletter,
        operator_emails=("operator@example.com",),
    )
    renewal_result = preclassify_gmail_thread(
        renewal,
        operator_emails=("operator@example.com",),
    )
    assert newsletter_result.should_detect is False
    assert newsletter_result.reason_code == "marketing_no_action"
    assert renewal_result.should_detect is True
    assert renewal_result.high_consequence is True


def test_detector_batches_threads_and_returns_validated_operational_candidates() -> None:
    incoming = thread(
        "ask",
        "Can you send the board deck?",
        (message("ask-1", "Can you please send the board deck by Friday?"),),
    )
    provider = FakeProvider(
        [
            {
                "threads": [
                    {
                        "thread_id": "ask",
                        "decision": "candidates",
                        "confidence": 0.94,
                        "candidates": [
                            {
                                "detector_key": "send-board-deck",
                                "operation": "create",
                                "kind": "commitment",
                                "title": "Send the board deck",
                                "owner": "operator",
                                "priority": "high",
                                "confidence": 0.94,
                                "due_at": None,
                                "starts_at": None,
                                "ends_at": None,
                                "expires_at": None,
                                "counterparty": "person@example.com",
                                "evidence_message_ids": ["ask-1"],
                                "handled_verdict": "needs_action",
                                "handled_confidence": 0.96,
                                "reason": "Direct unanswered request.",
                                "reconciliation_status": "confirmed",
                            }
                        ],
                    }
                ]
            }
        ]
    )
    result = detect_gmail_threads(
        [incoming],
        operator_emails=("operator@example.com",),
        timezone_name="America/Los_Angeles",
        policy_version="operations-v1@1",
        budget=GmailDetectorBudget(),
        llm_provider=provider,
    )
    assert provider.calls == 1
    assert result.coverage_complete is True
    assert result.requests == 1
    detection = result.detections[0]
    assert detection.disposition == "surfaced"
    assert detection.candidates[0].priority == 70
    assert detection.candidates[0].handled_verdict == "needs_action"
    assert detection.candidates[0].evidence_message_ids == ("ask-1",)


def test_high_consequence_model_ignore_becomes_visible_uncertain_attention() -> None:
    security = thread(
        "security",
        "Urgent security alert",
        (message("security-1", "Urgent security alert: review this change."),),
        message_class="transactional",
    )
    provider = FakeProvider(
        [
            {
                "threads": [
                    {
                        "thread_id": "security",
                        "decision": "ignore",
                        "reason_code": "model_ignore",
                        "confidence": 0.8,
                        "candidates": [],
                    }
                ]
            }
        ]
    )
    result = detect_gmail_threads(
        [security],
        operator_emails=("operator@example.com",),
        timezone_name="America/Los_Angeles",
        policy_version="operations-v1@1",
        budget=GmailDetectorBudget(),
        llm_provider=provider,
    )
    detection = result.detections[0]
    assert detection.disposition == "surfaced"
    assert detection.reason_code == "high_consequence_model_uncertain"
    assert detection.candidates[0].kind == "attention"
    assert detection.candidates[0].priority == 90
    assert detection.candidates[0].handled_verdict == "unknown"


def test_budget_overflow_is_visible_and_never_silently_dropped() -> None:
    incoming = thread(
        "large",
        "Can you review this?",
        (message("large-1", "Can you review this? " + ("x" * 5000)),),
    )
    provider = FakeProvider([])
    result = detect_gmail_threads(
        [incoming],
        operator_emails=("operator@example.com",),
        timezone_name="America/Los_Angeles",
        policy_version="operations-v1@1",
        budget=GmailDetectorBudget(
            max_calls=1,
            max_input_tokens=1,
            max_total_tokens=1,
            max_batch_threads=1,
            max_batch_chars=10_000,
        ),
        llm_provider=provider,
    )
    assert provider.calls == 0
    assert result.coverage_complete is False
    assert result.deferred_count == 1
    assert result.detections[0].disposition == "deferred"
    assert result.detections[0].reason_code == "detector_budget_exhausted"
