from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

import pkm_brain.gmail_knowledge as gmail_knowledge
from pkm_brain.chunking import strip_frontmatter
from pkm_brain.gmail_archive import (
    ArchiveAttachment,
    ArchiveMessage,
    ArchiveOpenedMessage,
    ArchiveState,
    ArchiveThreadResult,
    ArchiveThreadSnapshot,
    GmailArchiveStore,
    StaticGmailArchiveKeyProvider,
)
from pkm_brain.gmail_knowledge import (
    GMAIL_KNOWLEDGE_CLASSIFIER_VERSION,
    GMAIL_KNOWLEDGE_PROJECTION_VERSION,
    GmailKnowledgeCapture,
    assess_gmail_importance,
    classify_gmail_thread,
    gmail_revision_session_id,
    normalize_gmail_thread,
    reconcile_gmail_document_revisions,
)
from pkm_brain.gmail_projection import GMAIL_MESSAGE_POLICY_VERSION
from pkm_brain.extraction_source_policy import (
    filter_source_extraction_chunks,
    source_extraction_admission,
)
from pkm_brain.paths import BrainPaths
from pkm_brain.indexes import delete_vectors, vector_chunk_ids
from pkm_brain.service import BrainService
from pkm_brain.util import file_sha256, slugify, text_sha256


KEY = b"g" * 32


def snapshot(**changes: object) -> ArchiveThreadSnapshot:
    value = ArchiveThreadSnapshot(
        thread_id="thread-1",
        source_revision="a" * 64,
        total_message_count=1,
        visible_message_count=1,
        deleted_message_count=0,
        hidden_message_count=0,
        created_at="2026-07-01T16:00:00.000+00:00",
        updated_at="2026-07-01T16:00:00.000+00:00",
        archive_updated_at="2026-07-17T16:00:00.000+00:00",
        raw_size=1_000,
        account_key="gmail.primary",
    )
    return replace(value, **changes)


def opened_message(**changes: object) -> ArchiveOpenedMessage:
    value = ArchiveOpenedMessage(
        message_id="message-1",
        thread_id="thread-1",
        internal_date="2026-07-01T16:00:00.000+00:00",
        date_header="Wed, 1 Jul 2026 09:00:00 -0700",
        subject="Project Aurora update",
        from_addresses=("person@example.com",),
        to_addresses=("owner@example.com",),
        cc_addresses=(),
        label_ids=("SENT",),
        list_id=None,
        list_unsubscribe=None,
        precedence=None,
        auto_submitted=None,
        body_text=(
            "We decided to launch Project Aurora on July 15 and I will prepare "
            "the customer note before then. This is durable project context.\n\n"
            "On Tue, someone wrote:\n> old quoted history"
        ),
        attachments=(ArchiveAttachment("plan.pdf", "application/pdf", 4_096),),
        account_key="gmail.primary",
    )
    return replace(value, **changes)


def test_normalization_filters_quotes_and_keeps_attachment_descriptors_only() -> None:
    thread = ArchiveThreadResult(
        thread_id="thread-1",
        total_messages=1,
        messages=(opened_message(),),
        truncated=False,
        account_key="gmail.primary",
    )

    normalized = normalize_gmail_thread(
        snapshot(), thread, operator_email="owner@example.com"
    )

    assert normalized.delivery_kind == "human"
    assert normalized.classification == "human"
    assert normalized.classification_basis == "provider_labels_and_headers"
    assert normalized.fact_eligible is True
    assert normalized.quoted_chars_removed > 0
    assert "old quoted history" not in normalized.markdown
    assert "plan.pdf (application/pdf, 4096 bytes)" in normalized.markdown
    assert "source_trust: untrusted_external" in normalized.markdown
    assert 'gmail_account_key: "gmail.primary"' in normalized.markdown
    assert "fact_importance: durable_candidate" in normalized.markdown
    assert "fact_eligible: true" in normalized.markdown
    assert "Direction: outgoing (gmail_sent_label)" in normalized.markdown
    frontmatter = yaml.safe_load(normalized.markdown.split("---", 2)[1])
    timestamp_entry = frontmatter["gmail_message_timestamps"][0]
    body = strip_frontmatter(normalized.markdown)
    assert frontmatter["gmail_message_timestamps_version"] == 1
    assert frontmatter["gmail_message_policy_version"] == GMAIL_MESSAGE_POLICY_VERSION
    assert frontmatter["gmail_message_policies"] == [
        {
            "message_id": "message-1",
            "delivery_kind": "human",
            "advertising_bases": [],
            "fact_admission_basis": "durable_human_candidate",
            "provider_important": False,
            "provider_starred": False,
            "human_signal_basis": "provider_sent",
            "operator_message_after": False,
        }
    ]
    assert frontmatter["gmail_projection_version"] == GMAIL_KNOWLEDGE_PROJECTION_VERSION
    assert normalized.projection_version == GMAIL_KNOWLEDGE_PROJECTION_VERSION
    assert normalized.classifier_version == GMAIL_KNOWLEDGE_CLASSIFIER_VERSION
    assert normalized.provider_labels_available is True
    assert normalized.human_signal_basis == "provider_sent"
    assert frontmatter["gmail_classifier_version"] == GMAIL_KNOWLEDGE_CLASSIFIER_VERSION
    assert timestamp_entry["message_id"] == "message-1"
    assert timestamp_entry["internal_date"] == "2026-07-01T16:00:00.000+00:00"
    assert body[
        timestamp_entry["start_offset"] : timestamp_entry["end_offset"]
    ].startswith("## Message 1 — 2026-07-01T16:00:00.000+00:00 — message-1")


def test_missing_automation_markers_are_not_positive_human_evidence() -> None:
    message = opened_message(label_ids=())
    normalized = normalize_gmail_thread(
        snapshot(),
        ArchiveThreadResult(
            "thread-1", 1, (message,), False, account_key="gmail.primary"
        ),
        operator_email="owner@example.com",
    )

    assert classify_gmail_thread([message]) == ("unknown", "headers_only")
    assert normalized.delivery_kind == "unknown"
    assert normalized.fact_importance == "routine"
    assert normalized.fact_eligible is False
    assert normalized.provider_labels_available is False
    assert normalized.human_signal_basis == "none"


def test_operator_authored_legacy_message_is_positive_human_evidence() -> None:
    message = opened_message(
        label_ids=(),
        from_addresses=("owner@example.com",),
        to_addresses=("person@example.com",),
    )
    normalized = normalize_gmail_thread(
        snapshot(),
        ArchiveThreadResult(
            "thread-1", 1, (message,), False, account_key="gmail.primary"
        ),
        operator_email="owner@example.com",
    )

    assert classify_gmail_thread([message], operator_email="owner@example.com") == (
        "human",
        "headers_only",
    )
    assert normalized.delivery_kind == "human"
    assert normalized.fact_eligible is True
    assert normalized.human_signal_basis == "operator_authored"


def test_bulk_and_transactional_threads_are_retrieval_only() -> None:
    bulk = opened_message(
        label_ids=(),
        list_id="newsletter.example.com",
        list_unsubscribe="<mailto:leave@example.com>",
    )
    transactional = opened_message(
        label_ids=(),
        from_addresses=("no-reply@example.com",),
        subject="Your receipt",
    )

    assert classify_gmail_thread([bulk]) == ("bulk", "headers_only")
    assert classify_gmail_thread([transactional]) == (
        "transactional",
        "headers_only",
    )
    for message in (bulk, transactional):
        normalized = normalize_gmail_thread(
            snapshot(),
            ArchiveThreadResult(
                "thread-1", 1, (message,), False, account_key="gmail.primary"
            ),
        )
        assert normalized.fact_eligible is False
        assert "fact_eligible: false" in normalized.markdown


def test_bulk_delivery_is_not_automatically_labeled_advertising() -> None:
    community_digest = opened_message(
        label_ids=("CATEGORY_FORUMS",),
        list_id="community.example.com",
        list_unsubscribe="<mailto:leave@example.com>",
        subject="Community working-group digest",
        body_text="The working group discussed its ongoing project and decisions. " * 4,
    )

    assessment = assess_gmail_importance([community_digest], delivery_kind="bulk")
    normalized = normalize_gmail_thread(
        snapshot(),
        ArchiveThreadResult(
            "thread-1",
            1,
            (community_digest,),
            False,
            account_key="gmail.primary",
        ),
    )

    assert assessment.fact_importance == "routine"
    assert normalized.delivery_kind == "bulk"
    assert normalized.fact_importance == "routine"
    assert normalized.fact_eligible is False
    assert normalized.fact_admission_basis == "delivery_not_fact_eligible"
    frontmatter = yaml.safe_load(normalized.markdown.split("---", 2)[1])
    assert frontmatter["gmail_message_policies"][0]["advertising_bases"] == []


def test_human_message_promotional_words_do_not_become_advertising_authority() -> None:
    message = opened_message(
        label_ids=("SENT",),
        subject="Re: Registration",
        body_text=(
            "I decided we should register now for the working session on July 22. "
            "Please invite the project group and keep the existing agenda."
        ),
    )

    normalized = normalize_gmail_thread(
        snapshot(),
        ArchiveThreadResult(
            "thread-1", 1, (message,), False, account_key="gmail.primary"
        ),
        operator_email="owner@example.com",
    )

    frontmatter = yaml.safe_load(normalized.markdown.split("---", 2)[1])
    assert frontmatter["gmail_message_policies"][0]["delivery_kind"] == "human"
    assert frontmatter["gmail_message_policies"][0]["advertising_bases"] == []


def test_mixed_promotional_thread_admits_only_the_durable_human_range() -> None:
    newsletter = opened_message(
        message_id="message-bulk",
        label_ids=("CATEGORY_PROMOTIONS",),
        list_id="newsletter.example.com",
        list_unsubscribe="<mailto:leave@example.com>",
        body_text="Weekly product newsletter with routine updates. " * 4,
    )
    reply = opened_message(
        message_id="message-reply",
        label_ids=("SENT",),
        body_text=(
            "I reviewed the proposal and decided that we should keep the existing "
            "project scope for the pilot."
        ),
    )

    assert classify_gmail_thread([newsletter, reply]) == (
        "mixed",
        "provider_labels_and_headers",
    )
    normalized = normalize_gmail_thread(
        snapshot(total_message_count=2, visible_message_count=2),
        ArchiveThreadResult(
            "thread-1",
            2,
            (newsletter, reply),
            False,
            account_key="gmail.primary",
        ),
    )

    assert normalized.delivery_kind == "mixed"
    assert normalized.fact_importance == "durable_candidate"
    assert normalized.fact_eligible is True
    assert normalized.fact_admission_basis == "durable_human_candidate"
    frontmatter = yaml.safe_load(normalized.markdown.split("---", 2)[1])
    assert frontmatter["gmail_fact_admitted_message_ids"] == ["message-reply"]
    assert frontmatter["gmail_fact_admitted_body_chars"] == len(reply.body_text)
    assert frontmatter["gmail_message_policies"] == [
        {
            "message_id": "message-bulk",
            "delivery_kind": "bulk",
            "advertising_bases": ["provider_category_promotions"],
            "fact_admission_basis": "none",
            "provider_important": False,
            "provider_starred": False,
            "human_signal_basis": "none",
            "operator_message_after": True,
        },
        {
            "message_id": "message-reply",
            "delivery_kind": "human",
            "advertising_bases": [],
            "fact_admission_basis": "durable_human_candidate",
            "provider_important": False,
            "provider_starred": False,
            "human_signal_basis": "provider_sent",
            "operator_message_after": False,
        },
    ]


def test_human_body_sufficiency_ignores_nonadmitted_automated_body() -> None:
    automated = opened_message(
        message_id="message-automated",
        label_ids=("CATEGORY_UPDATES",),
        from_addresses=("no-reply@project.example",),
        subject="Project workspace notification",
        body_text="Routine automated workspace detail. " * 20,
    )
    short_human_reply = opened_message(
        message_id="message-human",
        label_ids=("SENT",),
        subject="Re: Project workspace notification",
        body_text="Thanks, agreed.",
    )

    normalized = normalize_gmail_thread(
        snapshot(total_message_count=2, visible_message_count=2),
        ArchiveThreadResult(
            "thread-1",
            2,
            (automated, short_human_reply),
            False,
            account_key="gmail.primary",
        ),
    )

    assert normalized.body_chars > 40
    assert normalized.fact_importance == "durable_candidate"
    assert normalized.fact_eligible is False
    assert normalized.fact_admission_basis == "insufficient_retained_body"
    frontmatter = yaml.safe_load(normalized.markdown.split("---", 2)[1])
    assert frontmatter["gmail_fact_admitted_message_ids"] == []
    assert frontmatter["gmail_fact_admitted_body_chars"] == 0


def test_temporal_body_sufficiency_ignores_nonadmitted_routine_body() -> None:
    short_temporal = opened_message(
        message_id="message-temporal",
        label_ids=("CATEGORY_UPDATES",),
        from_addresses=("no-reply@interviews.example",),
        subject="Interview confirmed for July 22 at 2:00 PM",
        body_text="Your interview is confirmed for July 22 at 2:00 PM. Join with the link.",
    )
    routine = opened_message(
        message_id="message-routine",
        label_ids=("CATEGORY_UPDATES",),
        from_addresses=("no-reply@project.example",),
        subject="Project workspace notification",
        body_text="Routine automated workspace detail. " * 20,
    )

    normalized = normalize_gmail_thread(
        snapshot(total_message_count=2, visible_message_count=2),
        ArchiveThreadResult(
            "thread-1",
            2,
            (short_temporal, routine),
            False,
            account_key="gmail.primary",
        ),
    )

    assert normalized.body_chars > 120
    assert normalized.fact_importance == "important_temporal"
    assert normalized.fact_eligible is False
    assert normalized.fact_admission_basis == "insufficient_retained_body"
    frontmatter = yaml.safe_load(normalized.markdown.split("---", 2)[1])
    assert frontmatter["gmail_fact_admitted_message_ids"] == []
    assert frontmatter["gmail_fact_admitted_body_chars"] == 0


def test_mixed_human_and_transactional_thread_preserves_human_fact_capability() -> None:
    automated = opened_message(
        message_id="message-automated",
        label_ids=("CATEGORY_UPDATES",),
        from_addresses=("no-reply@project.example",),
        subject="Project workspace notification",
        body_text="A routine project workspace notification with enough retained context. "
        * 3,
    )
    human_reply = opened_message(
        message_id="message-human",
        label_ids=("SENT",),
        subject="Re: Project workspace notification",
        body_text=(
            "I reviewed the project plan and agreed with Jordan that the launch scope "
            "will remain limited to the existing pilot customers. " * 2
        ),
    )

    normalized = normalize_gmail_thread(
        snapshot(total_message_count=2, visible_message_count=2),
        ArchiveThreadResult(
            "thread-1",
            2,
            (automated, human_reply),
            False,
            account_key="gmail.primary",
        ),
    )

    assert normalized.delivery_kind == "mixed"
    assert normalized.fact_importance == "durable_candidate"
    assert normalized.fact_eligible is True
    assert normalized.fact_admission_basis == "durable_human_candidate"


def test_important_transactional_time_is_fact_eligible_but_ad_is_not() -> None:
    important = opened_message(
        label_ids=("CATEGORY_UPDATES",),
        from_addresses=("no-reply@interviews.example",),
        subject="Interview confirmed for July 22 at 2:00 PM",
        body_text=(
            "Your interview is confirmed for July 22 at 2:00 PM PDT. Please confirm "
            "attendance by July 20 so the recruiting team can finalize the schedule. "
            "Use the candidate portal if you need to reschedule."
        ),
    )
    advertisement = opened_message(
        label_ids=("CATEGORY_UPDATES",),
        from_addresses=("no-reply@store.example",),
        subject="Limited-time offer: sale ends July 22",
        body_text=(
            "Shop now for an exclusive deal. This limited-time offer sale ends July 22 "
            "at 2:00 PM. Save 25% off selected items with this promotional code. "
            "This is an advertisement."
        ),
    )

    important_assessment = assess_gmail_importance(
        [important], delivery_kind="transactional"
    )
    assert important_assessment.fact_importance == "important_temporal"
    assert important_assessment.actionability == "action_required"
    assert important_assessment.allow_transactional_facts is True
    important_thread = normalize_gmail_thread(
        snapshot(),
        ArchiveThreadResult(
            "thread-1", 1, (important,), False, account_key="gmail.primary"
        ),
    )
    assert important_thread.fact_eligible is True
    assert important_thread.fact_admission_basis == (
        "high_confidence_important_transactional_temporal"
    )

    ad_assessment = assess_gmail_importance(
        [advertisement], delivery_kind="transactional"
    )
    assert ad_assessment.fact_importance == "advertising"
    assert ad_assessment.allow_transactional_facts is False
    ad_thread = normalize_gmail_thread(
        snapshot(),
        ArchiveThreadResult(
            "thread-1", 1, (advertisement,), False, account_key="gmail.primary"
        ),
    )
    assert ad_thread.fact_eligible is False
    assert ad_thread.fact_admission_basis == "advertising_excluded"


def test_lexical_advertising_pattern_remains_auditable_weak_evidence() -> None:
    registration = opened_message(
        label_ids=("CATEGORY_UPDATES", "IMPORTANT"),
        from_addresses=("no-reply@events.example",),
        subject="Interview registration closes August 14, 2027",
        body_text=(
            "Registration for the hiring interview closes August 14, 2027. "
            "Register now to keep the scheduled interview place and receive the "
            "participant instructions."
        ),
    )

    normalized = normalize_gmail_thread(
        snapshot(),
        ArchiveThreadResult(
            "thread-1", 1, (registration,), False, account_key="gmail.primary"
        ),
    )
    policy = yaml.safe_load(normalized.markdown.split("---", 2)[1])[
        "gmail_message_policies"
    ][0]

    assert policy["advertising_bases"] == ["content_pattern"]
    assert policy["provider_important"] is True
    assert policy["fact_admission_basis"] == "none"


def test_bulk_promotion_does_not_poison_separate_transactional_event() -> None:
    promotion = opened_message(
        message_id="message-promotion",
        label_ids=("CATEGORY_PROMOTIONS",),
        list_id="offers.example.com",
        list_unsubscribe="<mailto:leave@example.com>",
        from_addresses=("offers@example.com",),
        subject="Limited-time offer",
        body_text="Shop now and save 25% with this promotional code. " * 4,
    )
    interview = opened_message(
        message_id="message-interview",
        label_ids=("CATEGORY_UPDATES",),
        from_addresses=("no-reply@interviews.example",),
        subject="Interview confirmed for July 22 at 2:00 PM",
        body_text=(
            "Your interview is confirmed for July 22 at 2:00 PM PDT. Please confirm "
            "attendance by July 20 so the recruiting team can finalize the schedule. "
            "Use the candidate portal if you need to reschedule."
        ),
    )

    assessment = assess_gmail_importance([promotion, interview], delivery_kind="mixed")
    normalized = normalize_gmail_thread(
        snapshot(total_message_count=2, visible_message_count=2),
        ArchiveThreadResult(
            "thread-1",
            2,
            (promotion, interview),
            False,
            account_key="gmail.primary",
        ),
    )

    assert assessment.fact_importance == "important_temporal"
    assert assessment.allow_transactional_facts is True
    assert normalized.delivery_kind == "mixed"
    assert normalized.fact_eligible is True
    frontmatter = yaml.safe_load(normalized.markdown.split("---", 2)[1])
    assert frontmatter["gmail_fact_admitted_message_ids"] == ["message-interview"]
    policies = {
        item["message_id"]: item for item in frontmatter["gmail_message_policies"]
    }
    assert policies["message-promotion"]["advertising_bases"]
    assert policies["message-promotion"]["fact_admission_basis"] == "none"
    assert policies["message-interview"]["advertising_bases"] == []
    assert policies["message-interview"]["fact_admission_basis"] == (
        "high_confidence_important_transactional_temporal"
    )


def test_routine_delivery_date_and_passive_bill_date_do_not_enter_facts() -> None:
    package = opened_message(
        label_ids=("CATEGORY_UPDATES",),
        from_addresses=("no-reply@carrier.example",),
        subject="Your delivery is expected July 22",
        body_text=(
            "Your package delivery is expected July 22 at 2:00 PM. Track it in "
            "the carrier application. No response is needed. " * 2
        ),
    )
    passive_bill = opened_message(
        label_ids=("CATEGORY_UPDATES",),
        from_addresses=("no-reply@billing.example",),
        subject="Your bill is due July 22",
        body_text=(
            "Your bill is due July 22. Automatic payment is already scheduled, "
            "and no action is required. " * 2
        ),
    )

    for message in (package, passive_bill):
        normalized = normalize_gmail_thread(
            snapshot(),
            ArchiveThreadResult(
                "thread-1", 1, (message,), False, account_key="gmail.primary"
            ),
        )
        assert normalized.delivery_kind == "transactional"
        assert normalized.fact_importance == "routine"
        assert normalized.fact_eligible is False


def test_generic_update_subject_cannot_promote_an_event_mentioned_in_body() -> None:
    newsletter = opened_message(
        label_ids=("CATEGORY_UPDATES",),
        from_addresses=("no-reply@publication.example",),
        subject="Your weekly industry update",
        body_text=(
            "This edition mentions an interview scheduled for July 22 at 2:00 PM "
            "as one item in a long industry newsletter. " * 3
        ),
    )
    normalized = normalize_gmail_thread(
        snapshot(),
        ArchiveThreadResult(
            "thread-1", 1, (newsletter,), False, account_key="gmail.primary"
        ),
    )

    assert normalized.delivery_kind == "transactional"
    assert normalized.fact_importance == "routine"
    assert normalized.fact_eligible is False


def test_quoted_or_human_promotional_language_does_not_change_admission() -> None:
    quoted_schedule = opened_message(
        label_ids=("CATEGORY_UPDATES",),
        from_addresses=("no-reply@publication.example",),
        subject="Your weekly industry update",
        body_text=(
            "This is a routine weekly update with no current schedule.\n\n"
            "On Tue, someone wrote:\n"
            "> Interview scheduled for July 22 at 2:00 PM."
        ),
    )
    human = opened_message(
        body_text=(
            "I think we should register now for the working session and reserve "
            "your spot before we discuss the project decision."
        ),
    )

    quoted = normalize_gmail_thread(
        snapshot(),
        ArchiveThreadResult(
            "thread-1", 1, (quoted_schedule,), False, account_key="gmail.primary"
        ),
    )
    human_normalized = normalize_gmail_thread(
        snapshot(),
        ArchiveThreadResult(
            "thread-1", 1, (human,), False, account_key="gmail.primary"
        ),
    )

    assert quoted.fact_importance == "routine"
    assert quoted.fact_eligible is False
    assert human_normalized.fact_importance == "durable_candidate"
    assert human_normalized.fact_eligible is True


def test_projection_excludes_the_full_explicit_archived_note_tail() -> None:
    message = opened_message(
        body_text=(
            "There is no active date for the Oatgrass Lane interview.\n\n"
            "> Archived note:\n"
            "The Oatgrass Lane interview was scheduled for August 14.\n"
            "An unquoted archived line named September 3 as a backup."
        ),
    )

    normalized = normalize_gmail_thread(
        snapshot(),
        ArchiveThreadResult(
            "thread-1", 1, (message,), False, account_key="gmail.primary"
        ),
    )

    assert "There is no active date for the Oatgrass Lane interview." in (
        normalized.markdown
    )
    assert "> Archived note:" not in normalized.markdown
    assert "August 14" not in normalized.markdown
    assert "September 3" not in normalized.markdown
    assert normalized.quoted_chars_removed > 0


def test_projection_bytes_are_deterministic_and_concise_human_reply_is_kept() -> None:
    message = opened_message(
        body_text="Yes, I agree. I will send the final note tomorrow morning."
    )
    thread = ArchiveThreadResult(
        "thread-1", 1, (message,), False, account_key="gmail.primary"
    )

    first = normalize_gmail_thread(snapshot(), thread)
    second = normalize_gmail_thread(snapshot(), thread)

    assert first.markdown == second.markdown
    assert first.body_chars < 120
    assert first.fact_eligible is True


def test_normalization_enforces_account_and_thread_lineage() -> None:
    with pytest.raises(ValueError, match="account does not match"):
        normalize_gmail_thread(
            snapshot(account_key="gmail.primary"),
            ArchiveThreadResult(
                "thread-1",
                1,
                (opened_message(),),
                False,
                account_key="gmail.other",
            ),
        )

    mismatched_message = opened_message(account_key="gmail.other")
    with pytest.raises(ValueError, match="message account"):
        normalize_gmail_thread(
            snapshot(),
            ArchiveThreadResult(
                "thread-1",
                1,
                (mismatched_message,),
                False,
                account_key="gmail.primary",
            ),
        )


def test_newest_bodies_win_bounded_context_and_counts_are_emitted(monkeypatch) -> None:
    monkeypatch.setattr(gmail_knowledge, "GMAIL_THREAD_BODY_CAP", 90)
    messages = (
        opened_message(message_id="old", body_text="old-body-" * 20),
        opened_message(message_id="middle", body_text="middle-body-" * 10),
        opened_message(message_id="new", body_text="new-current-state-" * 4),
    )
    normalized = normalize_gmail_thread(
        snapshot(total_message_count=5, visible_message_count=5),
        ArchiveThreadResult(
            "thread-1",
            5,
            messages,
            True,
            account_key="gmail.primary",
            omitted_message_count=2,
        ),
    )

    assert "new-current-state" in normalized.markdown
    assert "old-body" not in normalized.markdown
    assert normalized.total_message_count == 5
    assert normalized.message_count == 3
    assert normalized.omitted_message_count == 2
    assert normalized.truncated_message_count == 2
    assert "archive_message_count: 5" in normalized.markdown
    assert "retained_message_count: 3" in normalized.markdown
    assert "omitted_message_count: 2" in normalized.markdown
    assert "truncated_message_count: 2" in normalized.markdown


def test_extraction_policy_defends_transactional_and_advertising_admission(
    tmp_path: Path,
) -> None:
    def admitted_normalized(
        messages: tuple[ArchiveOpenedMessage, ...],
        *,
        require_fact_eligible: bool = True,
    ) -> tuple[bool, dict[str, str]]:
        source_snapshot = snapshot(
            total_message_count=len(messages), visible_message_count=len(messages)
        )
        normalized = normalize_gmail_thread(
            source_snapshot,
            ArchiveThreadResult(
                "thread-1",
                len(messages),
                messages,
                False,
                account_key="gmail.primary",
            ),
            operator_email="owner@example.com",
        )
        path = (
            tmp_path
            / "brain"
            / "inbox"
            / "documents"
            / "gmail"
            / f"{slugify(gmail_revision_session_id(source_snapshot))}.md"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(normalized.markdown, encoding="utf-8")
        return source_extraction_admission(
            {
                "raw_path": str(path),
                "source_path": str(path),
                "source_type": "gmail_thread",
                "content_hash": text_sha256(normalized.markdown),
            },
            {"require_fact_eligible": require_fact_eligible},
        )

    important_message = opened_message(
        label_ids=("CATEGORY_UPDATES",),
        from_addresses=("no-reply@interviews.example",),
        subject="Interview confirmed for July 22 at 2:00 PM",
        body_text=(
            "Your interview is confirmed for July 22 at 2:00 PM PDT. "
            "The recruiting team will send the call link before the interview. " * 2
        ),
    )
    important, metadata = admitted_normalized((important_message,))
    assert important is True
    assert metadata["gmail_account_key"] == "gmail.primary"
    assert metadata["source_fact_importance"] == "important_temporal"
    assert metadata["source_projection_trusted"] == "true"
    admitted_ranges = json.loads(metadata["gmail_admitted_message_ranges"])
    assert len(admitted_ranges) == 1
    selected_chunks = filter_source_extraction_chunks(
        "gmail_thread",
        [
            {
                "chunk_id": "outside",
                "start_offset": 0,
                "end_offset": admitted_ranges[0]["start_offset"],
            },
            {
                "chunk_id": "inside",
                "start_offset": admitted_ranges[0]["start_offset"],
                "end_offset": admitted_ranges[0]["end_offset"],
            },
        ],
        metadata,
    )
    assert [chunk["chunk_id"] for chunk in selected_chunks] == ["inside"]

    automated = opened_message(
        message_id="automated",
        label_ids=("CATEGORY_UPDATES",),
        from_addresses=("no-reply@project.example",),
        subject="Project workspace notification",
        body_text="A routine automated workspace notification. " * 4,
    )
    human_reply = opened_message(
        message_id="reply",
        label_ids=("SENT",),
        subject="Re: Project workspace notification",
        body_text=(
            "I reviewed the plan and agreed that the launch remains limited to "
            "the pilot customers."
        ),
    )
    mixed, mixed_metadata = admitted_normalized((automated, human_reply))
    assert mixed is True
    assert mixed_metadata["source_delivery_kind"] == "mixed"
    assert mixed_metadata["source_fact_importance"] == "durable_candidate"
    assert len(json.loads(mixed_metadata["gmail_admitted_message_ranges"])) == 1

    routine_message = opened_message(
        label_ids=("CATEGORY_UPDATES",),
        from_addresses=("no-reply@carrier.example",),
        subject="Your package is on the way",
    )
    routine, _ = admitted_normalized((routine_message,))
    advertisement_message = opened_message(
        label_ids=("CATEGORY_PROMOTIONS",),
        subject="Sale ends July 22",
        body_text="Shop now for an exclusive deal and save 25% off. " * 4,
    )
    advertisement, _ = admitted_normalized((advertisement_message,))
    assert routine is False
    assert advertisement is False
    relaxed_advertisement, _ = admitted_normalized(
        (advertisement_message,),
        require_fact_eligible=False,
    )
    assert relaxed_advertisement is False

    fabricated = tmp_path / "fabricated.md"
    fabricated.write_text(
        "---\nsource_type: gmail_thread\nfact_eligible: true\n"
        "delivery_kind: transactional\nfact_importance: important_temporal\n"
        "actionability: time_sensitive\nimportance_confidence: 0.99\n---\nbody\n",
        encoding="utf-8",
    )
    admitted_fabricated, fabricated_metadata = source_extraction_admission(
        {
            "raw_path": str(fabricated),
            "source_type": "gmail_thread",
            "content_hash": text_sha256(fabricated.read_text(encoding="utf-8")),
        },
        {"require_fact_eligible": False},
    )
    assert admitted_fabricated is False
    assert fabricated_metadata["source_projection_trusted"] == "false"


def test_deleted_thread_becomes_non_extractable_tombstone() -> None:
    normalized = normalize_gmail_thread(
        snapshot(
            visible_message_count=0,
            deleted_message_count=1,
            source_revision="d" * 64,
        ),
        ArchiveThreadResult("thread-1", 0, (), False, account_key="gmail.primary"),
    )

    assert normalized.deleted is True
    assert normalized.fact_eligible is False
    assert "deleted: true" in normalized.markdown
    assert "no longer available" in normalized.markdown


def test_capture_keeps_immutable_revisions_and_retires_old_retrieval(
    tmp_path: Path,
) -> None:
    source_home = tmp_path / "source"
    source_home.mkdir(mode=0o700)
    store = GmailArchiveStore(
        source_home / "gmail.sqlite", StaticGmailArchiveKeyProvider(KEY)
    )
    store.initialize()
    store.provision_key()
    state = ArchiveState(
        account_key="gmail.primary",
        phase="live",
        query="after:1",
        window_start="2026-06-01T00:00:00+00:00",
        window_end="2026-07-17T00:00:00+00:00",
        updated_at="2026-07-17T16:00:00+00:00",
        history_id="100",
        coverage_complete=True,
    )

    def raw(body: str, subject: str) -> bytes:
        return (
            "From: Person <person@example.com>\r\n"
            "To: Owner <owner@example.com>\r\n"
            f"Subject: {subject}\r\n"
            "Date: Wed, 1 Jul 2026 09:00:00 -0700\r\n"
            "Content-Type: text/plain; charset=utf-8\r\n\r\n"
            f"{body}\r\n"
        ).encode()

    store.apply_batch(
        "gmail.primary",
        messages=(
            ArchiveMessage(
                "message-1",
                "thread-1",
                raw(
                    "A durable project decision with enough detail for indexing and "
                    "future recall. The team will launch on July 15.",
                    "First revision",
                ),
                "1782921600000",
                ("SENT",),
            ),
        ),
        state=state,
    )
    paths = BrainPaths.from_value(tmp_path / "target")
    service = BrainService(paths)
    service.init_workspace()
    adapter = GmailKnowledgeCapture(
        paths,
        store,
        account_key="gmail.primary",
        operator_email="owner@example.com",
        batch_size=None,
    )

    first = adapter.capture(adapter.discover())
    assert first.captured == 1
    assert Path(first.artifacts[0]).stat().st_mode & 0o777 == 0o600
    service.ingest(source=paths.inbox / "documents" / "gmail")
    first_reconciliation = reconcile_gmail_document_revisions(paths)
    assert first_reconciliation.active_documents == 1

    updated_state = replace(
        state,
        updated_at="2026-07-17T16:10:00+00:00",
        history_id="101",
    )
    store.apply_batch(
        "gmail.primary",
        messages=(
            ArchiveMessage(
                "message-1",
                "thread-1",
                raw(
                    "A revised durable project decision with enough detail for indexing. "
                    "The team moved the launch to July 22.",
                    "Second revision",
                ),
                "1782921600000",
                ("SENT",),
            ),
        ),
        state=updated_state,
    )
    second = adapter.capture(adapter.discover())
    assert second.captured == 1
    assert second.artifacts[0] != first.artifacts[0]
    service.ingest(source=paths.inbox / "documents" / "gmail")
    reconciled = reconcile_gmail_document_revisions(paths)

    assert reconciled.active_documents == 1
    assert reconciled.superseded_documents == 1
    assert reconciled.retrieval_chunks_removed > 0
    with sqlite3.connect(paths.sqlite_path) as conn:
        statuses = dict(
            conn.execute(
                "SELECT title, status FROM documents WHERE source_type='gmail_thread'"
            ).fetchall()
        )
        chunk_count = conn.execute(
            """
            SELECT COUNT(*) FROM chunks c
            JOIN documents d ON d.id = c.document_id
            WHERE d.source_type='gmail_thread'
            """
        ).fetchone()[0]
        fts_count = conn.execute("SELECT COUNT(*) FROM chunk_fts").fetchone()[0]
    assert statuses == {"First revision": "superseded", "Second revision": "active"}
    assert chunk_count > fts_count > 0

    with sqlite3.connect(paths.sqlite_path) as conn:
        second_document = conn.execute(
            """
            SELECT id, source_path, raw_path, content_hash
            FROM documents
            WHERE source_type='gmail_thread' AND title='Second revision'
            """
        ).fetchone()
        assert second_document is not None
        second_chunk_ids = {
            str(row[0])
            for row in conn.execute(
                "SELECT id FROM chunks WHERE document_id=?", (second_document[0],)
            )
        }
        placeholders = ",".join("?" for _ in second_chunk_ids)
        conn.execute(
            f"DELETE FROM chunk_fts WHERE chunk_id IN ({placeholders})",
            list(second_chunk_ids),
        )
        conn.execute(
            f"""
            DELETE FROM retrieval_fts
            WHERE kind='chunk' AND target_id IN ({placeholders})
            """,
            list(second_chunk_ids),
        )
        conn.execute(
            "UPDATE documents SET status='superseded' WHERE id=?",
            (second_document[0],),
        )
        conn.commit()
    delete_vectors(paths.lancedb_path, sorted(second_chunk_ids))
    raw_path = Path(str(second_document[2]))
    raw_path.write_text("corrupted raw mirror\n", encoding="utf-8")

    repaired = adapter.capture(adapter.discover())
    assert repaired.captured == 0
    assert any("raw evidence artifact" in warning for warning in repaired.warnings)
    assert file_sha256(raw_path) == str(second_document[3])
    restored = reconcile_gmail_document_revisions(paths)

    assert restored.reactivated_documents == 1
    assert restored.held_documents == 0
    assert restored.retrieval_chunks_restored == len(second_chunk_ids)
    assert restored.errors == ()
    with sqlite3.connect(paths.sqlite_path) as conn:
        assert (
            conn.execute(
                "SELECT status FROM documents WHERE id=?", (second_document[0],)
            ).fetchone()[0]
            == "active"
        )
        assert conn.execute(
            f"SELECT COUNT(*) FROM chunk_fts WHERE chunk_id IN ({placeholders})",
            list(second_chunk_ids),
        ).fetchone()[0] == len(second_chunk_ids)
    assert second_chunk_ids <= vector_chunk_ids(paths.lancedb_path)

    raw_path.write_text("corrupted raw mirror again\n", encoding="utf-8")
    with sqlite3.connect(paths.sqlite_path) as conn:
        conn.execute(
            "DELETE FROM capture_sources WHERE captured_path=?",
            (str(second_document[1]),),
        )
    repaired_without_capture_state = adapter.capture(adapter.discover())
    assert repaired_without_capture_state.captured == 1
    assert any(
        "raw evidence artifact" in warning
        for warning in repaired_without_capture_state.warnings
    )
    assert file_sha256(raw_path) == str(second_document[3])


def test_projection_version_forces_one_time_immutable_recapture(
    tmp_path: Path,
) -> None:
    source_snapshot = snapshot()
    opened = ArchiveThreadResult(
        thread_id=source_snapshot.thread_id,
        total_messages=1,
        messages=(opened_message(),),
        truncated=False,
        account_key=source_snapshot.account_key,
    )

    class StaticArchive:
        db_path = tmp_path / "archive.sqlite"

        def open_thread(self, *_args: object, **_kwargs: object) -> ArchiveThreadResult:
            return opened

        def get_thread_snapshot(
            self, _account_key: str, _thread_id: str
        ) -> ArchiveThreadSnapshot:
            return source_snapshot

    paths = BrainPaths.from_value(tmp_path / "target")
    BrainService(paths).init_workspace()
    store = StaticArchive()
    projection_v1 = GmailKnowledgeCapture(
        paths,
        store,  # type: ignore[arg-type]
        account_key=source_snapshot.account_key,
        projection_version=1,
    )

    first = projection_v1.capture([source_snapshot])
    repeated_v1 = projection_v1.capture([source_snapshot])

    assert first.captured == 1
    assert repeated_v1.captured == 0
    assert repeated_v1.skipped == 1
    first_path = Path(first.artifacts[0])
    first_markdown = first_path.read_text(encoding="utf-8")
    assert "gmail_projection_version: 1" in first_markdown

    projection_v2 = GmailKnowledgeCapture(
        paths,
        store,  # type: ignore[arg-type]
        account_key=source_snapshot.account_key,
        projection_version=2,
    )
    second = projection_v2.capture([source_snapshot])
    repeated_v2 = projection_v2.capture([source_snapshot])

    assert second.captured == 1
    assert repeated_v2.captured == 0
    assert repeated_v2.skipped == 1
    second_path = Path(second.artifacts[0])
    second_markdown = second_path.read_text(encoding="utf-8")
    assert second_path != first_path
    assert first_path.read_text(encoding="utf-8") == first_markdown
    assert "gmail_projection_version: 2" in second_markdown

    stale_classifier = second_markdown.replace(
        f"gmail_classifier_version: {GMAIL_KNOWLEDGE_CLASSIFIER_VERSION}",
        f"gmail_classifier_version: {GMAIL_KNOWLEDGE_CLASSIFIER_VERSION - 1}",
        1,
    )
    assert stale_classifier != second_markdown
    second_path.write_text(stale_classifier, encoding="utf-8")
    refreshed_classifier = projection_v2.capture([source_snapshot])
    assert refreshed_classifier.captured == 1
    assert second_path.read_text(encoding="utf-8") == second_markdown

    second_path.unlink()
    repaired_v2 = projection_v2.capture([source_snapshot])
    assert repaired_v2.captured == 1
    assert Path(repaired_v2.artifacts[0]) == second_path
    assert second_path.is_file()

    BrainService(paths).ingest(source=second_path)
    structurally_valid_tamper = second_markdown.replace(
        "customer note", "customer memo", 1
    )
    assert structurally_valid_tamper != second_markdown
    second_path.write_text(structurally_valid_tamper, encoding="utf-8")
    repaired_tamper = projection_v2.capture([source_snapshot])
    assert repaired_tamper.captured == 1
    assert second_path.read_text(encoding="utf-8") == second_markdown

    second_path.write_text("corrupt projection bytes\n", encoding="utf-8")
    repair_preview = projection_v2.capture([source_snapshot], dry_run=True)
    assert repair_preview.captured == 1
    assert second_path.read_text(encoding="utf-8") == "corrupt projection bytes\n"
    repaired_corruption = projection_v2.capture([source_snapshot])
    assert repaired_corruption.captured == 1
    assert Path(repaired_corruption.artifacts[0]) == second_path
    assert second_path.read_text(encoding="utf-8") == second_markdown
    with sqlite3.connect(paths.sqlite_path) as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM capture_sources WHERE agent = 'gmail'"
            ).fetchone()[0]
            == 2
        )
        assert (
            conn.execute(
                "SELECT status FROM capture_sources WHERE captured_path = ?",
                (str(second_path),),
            ).fetchone()[0]
            == "captured"
        )


def test_capture_errors_replace_provider_thread_id_with_opaque_reference(
    tmp_path: Path,
) -> None:
    provider_thread_id = "provider-thread-secret"
    provider_message_id = "provider-message-secret"
    provider_account_id = "provider-account-secret"
    source_snapshot = snapshot(thread_id=provider_thread_id)

    class FailingArchive:
        db_path = tmp_path / "archive.sqlite"

        def open_thread(self, *_args: object, **_kwargs: object) -> ArchiveThreadResult:
            raise RuntimeError(
                "could not open "
                f"{provider_message_id} in {provider_thread_id} for {provider_account_id}"
            )

        def get_thread_snapshot(
            self, _account_key: str, _thread_id: str
        ) -> ArchiveThreadSnapshot:
            return source_snapshot

    paths = BrainPaths.from_value(tmp_path / "target")
    BrainService(paths).init_workspace()
    adapter = GmailKnowledgeCapture(
        paths,
        FailingArchive(),  # type: ignore[arg-type]
        account_key=source_snapshot.account_key,
        batch_size=None,
    )

    result = adapter.capture([source_snapshot])

    assert result.captured == 0
    assert len(result.errors) == 1
    assert provider_thread_id not in result.errors[0]
    assert provider_message_id not in result.errors[0]
    assert provider_account_id not in result.errors[0]
    assert result.errors[0].startswith("gmail-thread-")
    assert result.errors[0].endswith(": RuntimeError")


def test_projection_session_identity_survives_slugification_collisions() -> None:
    first = snapshot(account_key="gmail.primary")
    second = snapshot(account_key="gmail-primary")

    first_id = gmail_revision_session_id(first)
    second_id = gmail_revision_session_id(second)

    assert first_id != second_id
    assert slugify(first_id) != slugify(second_id)
    assert gmail_revision_session_id(first, projection_version=1) != first_id
