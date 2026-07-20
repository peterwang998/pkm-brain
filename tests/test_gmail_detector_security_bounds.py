from __future__ import annotations

import json

from pkm_brain.gmail_operations import GmailDetectorBudget, detect_gmail_threads
from pkm_brain.google_normalization import (
    NormalizedGmailMessage,
    NormalizedGmailThread,
)


class GuardProvider:
    name = "guard"
    model = "guard"

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def complete(self, prompt: str) -> str:
        self.prompts.append(prompt)
        assert "OVERSIZED_SECRET" not in prompt
        assert "123 456" not in prompt
        return json.dumps(
            {
                "threads": [
                    {
                        "thread_id": "small",
                        "decision": "ignore",
                        "reason_code": "model_ignore",
                        "confidence": 0.9,
                        "candidates": [],
                    }
                ]
            }
        )


def _thread(thread_id: str, body: str) -> NormalizedGmailThread:
    message = NormalizedGmailMessage(
        message_id=f"{thread_id}-message",
        thread_id=thread_id,
        internal_date="1783969200000",
        timestamp="2026-07-13T19:00:00+00:00",
        from_addresses=("person@example.com",),
        to_addresses=("operator@example.com",),
        cc_addresses=(),
        subject="Please review this",
        date_header=None,
        internet_message_id=None,
        in_reply_to=None,
        references=(),
        label_ids=("INBOX",),
        outgoing=False,
        operator_authored=False,
        body=body,
        body_kind="text/plain",
        attachment_count=0,
        quoted_chars_removed=0,
        truncated=False,
    )
    return NormalizedGmailThread(
        thread_id=thread_id,
        history_id="1",
        source_revision="1",
        subject="Please review this",
        created_at=message.timestamp,
        updated_at=message.timestamp,
        message_class="human",
        messages=(message,),
        body_chars=len(body),
        attachment_count=0,
        quoted_chars_removed=0,
        truncated=False,
    )


def test_single_oversized_thread_surfaces_without_crossing_batch_char_bound() -> None:
    provider = GuardProvider()
    oversized = _thread(
        "oversized",
        "Please review OVERSIZED_SECRET " + ("x" * 10_000),
    )
    small = _thread(
        "small",
        "Please review the attached plan by Friday. Verification code 123 456.",
    )

    result = detect_gmail_threads(
        [oversized, small],
        operator_emails=("operator@example.com",),
        timezone_name="America/Los_Angeles",
        policy_version="operations-v1@1",
        budget=GmailDetectorBudget(
            max_calls=2,
            max_input_tokens=100_000,
            max_total_tokens=100_000,
            max_batch_threads=2,
            max_batch_chars=5_000,
        ),
        llm_provider=provider,
    )

    assert len(provider.prompts) == 1
    assert all(len(prompt) <= 5_000 for prompt in provider.prompts)
    assert result.coverage_complete is True
    assert result.deferred_count == 0
    assert result.detections[0].disposition == "surfaced"
    assert result.detections[0].reason_code == "detector_prompt_oversized_uncertain"
    assert result.detections[0].candidates[0].reconciliation_status == "ambiguous"
    assert result.detections[1].disposition == "surfaced"
    assert result.detections[1].reason_code == "direct_obligation_model_uncertain"
