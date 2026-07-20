from __future__ import annotations

import pkm_brain.service as service_module
from pkm_brain.gmail_retrieval_policy import (
    gmail_document_tags,
    gmail_retrieval_noise_reasons,
)
from pkm_brain.paths import BrainPaths
from pkm_brain.service import BrainService


def test_gmail_frontmatter_becomes_retrieval_tags() -> None:
    tags = gmail_document_tags(
        """---
source_type: gmail_thread
delivery_kind: transactional
fact_importance: time_sensitive
actionability: action_required
fact_eligible: true
deleted: false
---
Body
""",
        "gmail_thread",
    )

    assert tags == [
        "gmail:delivery:transactional",
        "gmail:importance:time-sensitive",
        "gmail:actionability:action-required",
        "gmail:fact-eligible",
    ]


def test_bulk_gmail_is_suppressed_unless_query_explicitly_requests_it() -> None:
    candidate = {
        "source_type": "gmail_thread",
        "tags": '["gmail:delivery:bulk", "gmail:importance:advertising"]',
    }

    assert gmail_retrieval_noise_reasons(candidate, "Acme product strategy") == [
        "bulk or advertising Gmail thread"
    ]
    assert gmail_retrieval_noise_reasons(candidate, "Acme newsletter") == []


def test_generic_mail_words_do_not_unsuppress_bulk_or_advertising_gmail() -> None:
    candidate = {
        "source_type": "gmail_thread",
        "tags": '["gmail:delivery:bulk", "gmail:importance:advertising"]',
    }

    for query in (
        "prepare a meeting brief with email context",
        "proactively draft email responses",
        "organize meeting prep from the inbox",
        "search my mail for Acme",
    ):
        assert gmail_retrieval_noise_reasons(candidate, query) == [
            "bulk or advertising Gmail thread"
        ]


def test_explicit_mailbox_search_only_unsuppresses_routine_mail() -> None:
    routine = {
        "source_type": "gmail_thread",
        "tags": '["gmail:delivery:unknown", "gmail:importance:routine"]',
    }

    for query in (
        "search my mail for Acme",
        "show me emails from Acme",
        "emails regarding Acme",
    ):
        assert gmail_retrieval_noise_reasons(routine, query) == []
    assert gmail_retrieval_noise_reasons(routine, "draft an email response") == [
        "routine low-signal Gmail thread"
    ]


def test_human_and_important_transactional_gmail_remain_retrievable() -> None:
    for tags in (
        '["gmail:delivery:human"]',
        '["gmail:delivery:transactional", "gmail:importance:time-sensitive"]',
    ):
        assert (
            gmail_retrieval_noise_reasons(
                {"source_type": "gmail_thread", "tags": tags}, "upcoming interview"
            )
            == []
        )


def test_routine_unknown_and_transactional_mail_need_an_explicit_mail_query() -> None:
    for delivery in ("unknown", "transactional"):
        candidate = {
            "source_type": "gmail_thread",
            "tags": (
                f'["gmail:delivery:{delivery}", "gmail:importance:routine"]'
            ),
        }
        assert gmail_retrieval_noise_reasons(candidate, "product strategy") == [
            "routine low-signal Gmail thread"
        ]
        assert gmail_retrieval_noise_reasons(candidate, "search my mail") == []


def test_fanout_overfetches_before_filtering_low_signal_gmail(
    tmp_path, monkeypatch
) -> None:
    service = BrainService(BrainPaths.from_value(tmp_path / "brain"))
    lexical = [{"chunk_id": f"chunk-{index}"} for index in range(81)]
    rows = {
        f"chunk-{index}": {
            "chunk_id": f"chunk-{index}",
            "source_type": "gmail_thread",
            "tags": (
                '["gmail:delivery:bulk", "gmail:importance:advertising"]'
                if index < 70
                else '["gmail:delivery:human", "gmail:importance:durable-candidate"]'
            ),
        }
        for index in range(81)
    }

    def lexical_search(_query: str, limit: int):
        assert limit == 960
        return lexical

    monkeypatch.setattr(service, "_search_fts", lexical_search)
    monkeypatch.setattr(
        service,
        "_chunks_by_ids",
        lambda chunk_ids: [rows[chunk_id] for chunk_id in chunk_ids],
    )
    monkeypatch.setattr(service_module, "search_vectors", lambda *_args, **_kwargs: [])

    candidates, debug = service._fanout_chunk_candidates("project strategy", limit=60)

    assert candidates[0]["chunk_id"] == "chunk-70"
    assert all(row["chunk_id"] not in {f"chunk-{i}" for i in range(70)} for row in candidates)
    assert debug["candidate_ids"][0] == "chunk-70"


def test_fanout_overfetch_handles_a_high_suppression_ratio_within_its_cap(
    tmp_path, monkeypatch
) -> None:
    service = BrainService(BrainPaths.from_value(tmp_path / "brain"))
    noise_count = 900
    human_count = 60
    lexical = [
        {"chunk_id": f"chunk-{index}"} for index in range(noise_count + human_count)
    ]
    rows = {
        f"chunk-{index}": {
            "chunk_id": f"chunk-{index}",
            "source_type": "gmail_thread",
            "tags": (
                '["gmail:delivery:bulk", "gmail:importance:advertising"]'
                if index < noise_count
                else '["gmail:delivery:human", "gmail:importance:durable-candidate"]'
            ),
        }
        for index in range(noise_count + human_count)
    }

    def lexical_search(_query: str, limit: int):
        assert limit == 960
        return lexical[:limit]

    monkeypatch.setattr(service, "_search_fts", lexical_search)
    monkeypatch.setattr(
        service,
        "_chunks_by_ids",
        lambda chunk_ids: [rows[chunk_id] for chunk_id in chunk_ids],
    )
    monkeypatch.setattr(service_module, "search_vectors", lambda *_args, **_kwargs: [])

    candidates, debug = service._fanout_chunk_candidates("project strategy", limit=60)

    assert len(candidates) == human_count
    assert candidates[0]["chunk_id"] == f"chunk-{noise_count}"
    assert debug["candidate_ids"] == [
        f"chunk-{index}" for index in range(noise_count, noise_count + human_count)
    ]
