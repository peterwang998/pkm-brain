from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import pkm_brain.service as service_module
from pkm_brain.db import connection
from pkm_brain.paths import BrainPaths
from pkm_brain.service import (
    BrainService,
    ReadOnlyModeError,
    RetrospectiveEvidenceSource,
)


def _source(
    evidence_id: str,
    *,
    available_at: str,
    chunk_ids: tuple[str, ...],
) -> RetrospectiveEvidenceSource:
    return RetrospectiveEvidenceSource(
        evidence_id=evidence_id,
        available_at=available_at,
        chunk_ids=chunk_ids,
    )


def _service(tmp_path: Path) -> BrainService:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()
    return BrainService(paths, read_only=True)


def _install_source_ranking(
    monkeypatch: pytest.MonkeyPatch,
    service: BrainService,
    *,
    chunk_ids: tuple[str, ...],
    calls: list[dict[str, Any]],
) -> None:
    def fanout(query: str, *, limit: int) -> tuple[list[dict[str, Any]], dict]:
        calls.append({"fanout_query": query, "fanout_limit": limit})
        return ([{"chunk_id": chunk_id} for chunk_id in chunk_ids], {"fused": []})

    def rerank(
        query: str,
        candidates: list[dict[str, Any]],
        _fanout: dict[str, Any],
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        calls.append({"rerank_query": query, **kwargs})
        return candidates

    monkeypatch.setattr(service, "_fanout_chunk_candidates", fanout)
    monkeypatch.setattr(service_module, "rerank_chunks", rerank)
    monkeypatch.setattr(
        service_module,
        "select_search_results",
        lambda candidates, limit: candidates[:limit],
    )


def test_temporal_replay_is_fact_first_without_using_cutoff_as_known_time(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(tmp_path)
    calls: list[dict[str, Any]] = []
    _install_source_ranking(
        monkeypatch,
        service,
        chunk_ids=("chunk_source",),
        calls=calls,
    )

    def facts(query: str, **kwargs: Any) -> list[dict[str, Any]]:
        calls.append({"fact_query": query, **kwargs})
        return [
            {
                "source_spans": [{"chunk_id": "chunk_fact", "start": 0, "end": 4}],
                "source_ids": ["chunk:chunk_fact"],
            }
        ]

    monkeypatch.setattr(service, "search_facts", facts)
    result = service.retrieve_retrospective_evidence(
        "what changed?",
        evidence_sources=(
            _source(
                "evidence-source",
                available_at="2026-07-01T10:00:00Z",
                chunk_ids=("chunk_source",),
            ),
            _source(
                "evidence-fact",
                available_at="2026-07-01T09:00:00Z",
                chunk_ids=("chunk_fact",),
            ),
        ),
        source_available_as_of="2026-07-02T00:00:00-04:00",
        context_text="prior message",
    )

    assert result == {
        "version": "retrospective_evidence_retrieval_v1",
        "retrieval_arm": "temporal",
        "source_available_as_of": "2026-07-02T04:00:00+00:00",
        "ranked_evidence_ids": ["evidence-fact", "evidence-source"],
        "persisted": False,
    }
    fact_call = next(call for call in calls if "fact_query" in call)
    assert fact_call["limit"] == 32
    assert fact_call["temporal_request"].known_as_of is None
    assert fact_call["temporal_request"].temporal_mode == "current"
    expected_query = "Context:\nprior message\n\nFollow-up question:\nwhat changed?"
    assert fact_call["fact_query"] == expected_query
    rerank_call = next(call for call in calls if "rerank_query" in call)
    assert rerank_call == {
        "rerank_query": expected_query,
        "lineage_scores": {},
        "apply_recency": False,
    }


def test_context_dates_and_lifecycle_language_cannot_select_temporal_intent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(tmp_path)
    calls: list[dict[str, Any]] = []
    _install_source_ranking(
        monkeypatch,
        service,
        chunk_ids=("chunk_answer",),
        calls=calls,
    )

    def facts(query: str, **kwargs: Any) -> list[dict[str, Any]]:
        calls.append({"fact_query": query, **kwargs})
        return []

    monkeypatch.setattr(service, "search_facts", facts)
    context = (
        "On 2024-03-01 the launch was scheduled for 2024-04-02, then "
        "cancelled. What did we know as of 2024-03-15?"
    )
    result = service.retrieve_retrospective_evidence(
        "Which message is relevant?",
        evidence_sources=(
            _source(
                "answer",
                available_at="2026-07-01T10:00:00Z",
                chunk_ids=("chunk_answer",),
            ),
        ),
        source_available_as_of="2026-07-02T00:00:00Z",
        context_text=context,
    )

    assert result["source_available_as_of"] == "2026-07-02T00:00:00+00:00"
    fact_call = next(call for call in calls if "fact_query" in call)
    assert fact_call["fact_query"] == (
        f"Context:\n{context}\n\nFollow-up question:\nWhich message is relevant?"
    )
    temporal = fact_call["temporal_request"]
    assert temporal.temporal_mode == "current"
    assert temporal.valid_as_of is None
    assert temporal.known_as_of is None
    assert temporal.event_as_of is None
    assert temporal.event_kind is None


def test_user_query_temporal_intent_ignores_conflicting_context_and_source_clock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(tmp_path)
    calls: list[dict[str, Any]] = []
    _install_source_ranking(
        monkeypatch,
        service,
        chunk_ids=("chunk_answer",),
        calls=calls,
    )

    def facts(query: str, **kwargs: Any) -> list[dict[str, Any]]:
        calls.append({"fact_query": query, **kwargs})
        return []

    monkeypatch.setattr(service, "search_facts", facts)
    service.retrieve_retrospective_evidence(
        "What was true as of 2025-05-06?",
        evidence_sources=(
            _source(
                "answer",
                available_at="2026-07-01T10:00:00Z",
                chunk_ids=("chunk_answer",),
            ),
        ),
        source_available_as_of="2026-07-02T00:00:00Z",
        context_text="What did we know as of 2024-03-15? The launch was cancelled.",
    )

    fact_call = next(call for call in calls if "fact_query" in call)
    temporal = fact_call["temporal_request"]
    assert temporal.temporal_mode == "valid"
    assert temporal.valid_as_of == "2025-05-06T23:59:59.999999+00:00"
    assert temporal.known_as_of is None
    assert temporal.event_as_of is None


def test_source_replay_disables_temporal_arm_and_never_persists(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(tmp_path)
    calls: list[dict[str, Any]] = []
    _install_source_ranking(
        monkeypatch,
        service,
        chunk_ids=("chunk_source",),
        calls=calls,
    )
    monkeypatch.setattr(
        service,
        "search_facts",
        lambda *_args, **_kwargs: pytest.fail("source arm searched facts"),
    )
    with connection(service.paths.sqlite_path) as conn:
        before = (
            conn.execute("SELECT COUNT(*) FROM retrieval_events").fetchone()[0],
            conn.execute("SELECT COUNT(*) FROM context_lineage_events").fetchone()[0],
        )

    result = service.retrieve_retrospective_evidence(
        "source-only question",
        evidence_sources=(
            _source(
                "evidence-source",
                available_at="2026-07-01T10:00:00Z",
                chunk_ids=("chunk_source",),
            ),
        ),
        source_available_as_of="2026-07-02T00:00:00Z",
        retrieval_arm="source",
    )

    with connection(service.paths.sqlite_path) as conn:
        after = (
            conn.execute("SELECT COUNT(*) FROM retrieval_events").fetchone()[0],
            conn.execute("SELECT COUNT(*) FROM context_lineage_events").fetchone()[0],
        )
    assert result["ranked_evidence_ids"] == ["evidence-source"]
    assert result["persisted"] is False
    assert after == before
    rerank_call = next(call for call in calls if "rerank_query" in call)
    assert rerank_call["lineage_scores"] == {}
    assert rerank_call["apply_recency"] is False


@pytest.mark.parametrize(
    "unsafe_fact",
    (
        {
            "source_spans": [
                {"chunk_id": "chunk_past"},
                {"chunk_id": "chunk_future"},
            ]
        },
        {
            "source_spans": [{"chunk_id": "chunk_past"}],
            "source_ids": ["document:unmappable"],
        },
        {"source_spans": [{"document_id": "document-only"}]},
    ),
)
def test_incomplete_or_future_fact_citations_discard_the_whole_fact_signal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    unsafe_fact: dict[str, Any],
) -> None:
    service = _service(tmp_path)
    calls: list[dict[str, Any]] = []
    _install_source_ranking(
        monkeypatch,
        service,
        chunk_ids=("chunk_fallback",),
        calls=calls,
    )
    monkeypatch.setattr(
        service, "search_facts", lambda *_args, **_kwargs: [unsafe_fact]
    )

    result = service.retrieve_retrospective_evidence(
        "citation safety",
        evidence_sources=(
            _source(
                "fact-past",
                available_at="2026-07-01T09:00:00Z",
                chunk_ids=("chunk_past",),
            ),
            _source(
                "fact-future",
                available_at="2026-07-03T09:00:00Z",
                chunk_ids=("chunk_future",),
            ),
            _source(
                "fallback",
                available_at="2026-07-01T10:00:00Z",
                chunk_ids=("chunk_fallback",),
            ),
        ),
        source_available_as_of="2026-07-02T00:00:00Z",
    )

    assert result["ranked_evidence_ids"] == ["fallback"]


def test_context_exclusion_and_shared_future_chunk_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(tmp_path)
    calls: list[dict[str, Any]] = []
    _install_source_ranking(
        monkeypatch,
        service,
        chunk_ids=("chunk_shared", "chunk_fallback"),
        calls=calls,
    )
    monkeypatch.setattr(
        service,
        "search_facts",
        lambda *_args, **_kwargs: [
            {
                "source_spans": [
                    {"chunk_id": "chunk_context"},
                    {"chunk_id": "chunk_answer"},
                ]
            }
        ],
    )

    result = service.retrieve_retrospective_evidence(
        "what changed?",
        evidence_sources=(
            _source(
                "context",
                available_at="2026-07-01T08:00:00Z",
                chunk_ids=("chunk_context",),
            ),
            _source(
                "answer",
                available_at="2026-07-01T09:00:00Z",
                chunk_ids=("chunk_answer",),
            ),
            _source(
                "shared-past",
                available_at="2026-07-01T10:00:00Z",
                chunk_ids=("chunk_shared",),
            ),
            _source(
                "shared-future",
                available_at="2026-07-03T10:00:00Z",
                chunk_ids=("chunk_shared",),
            ),
            _source(
                "fallback",
                available_at="2026-07-01T11:00:00Z",
                chunk_ids=("chunk_fallback",),
            ),
        ),
        source_available_as_of="2026-07-02T00:00:00Z",
        excluded_evidence_ids=("context",),
    )

    assert result["ranked_evidence_ids"] == ["answer", "fallback"]


@pytest.mark.parametrize(
    ("overrides", "error"),
    (
        ({"source_available_as_of": "2026-07-02"}, "must include a timezone"),
        ({"retrieval_arm": "unknown"}, "retrieval_arm"),
        ({"limit": 0}, "limit"),
        ({"limit": 11}, "limit"),
        ({"excluded_evidence_ids": ("missing",)}, "outside evidence authority"),
    ),
)
def test_retrospective_retrieval_rejects_ambiguous_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    overrides: dict[str, Any],
    error: str,
) -> None:
    service = _service(tmp_path)
    calls: list[dict[str, Any]] = []
    _install_source_ranking(monkeypatch, service, chunk_ids=(), calls=calls)
    kwargs: dict[str, Any] = {
        "evidence_sources": (
            _source(
                "evidence",
                available_at="2026-07-01T00:00:00Z",
                chunk_ids=("chunk_evidence",),
            ),
        ),
        "source_available_as_of": "2026-07-02T00:00:00Z",
        "retrieval_arm": "source",
    }
    kwargs.update(overrides)

    with pytest.raises(ValueError, match=error):
        service.retrieve_retrospective_evidence("question", **kwargs)


def test_retrospective_evidence_authority_rejects_duplicates(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    duplicate = _source(
        "evidence",
        available_at="2026-07-01T00:00:00Z",
        chunk_ids=("chunk_evidence",),
    )

    with pytest.raises(ValueError, match="duplicate evidence_id"):
        service.retrieve_retrospective_evidence(
            "question",
            evidence_sources=(duplicate, duplicate),
            source_available_as_of="2026-07-02T00:00:00Z",
            retrieval_arm="source",
        )

    with pytest.raises(ValueError, match="chunk_ids are invalid"):
        service.retrieve_retrospective_evidence(
            "question",
            evidence_sources=(
                _source(
                    "evidence",
                    available_at="2026-07-01T00:00:00Z",
                    chunk_ids=("chunk_evidence", "chunk_evidence"),
                ),
            ),
            source_available_as_of="2026-07-02T00:00:00Z",
            retrieval_arm="source",
        )


def test_retrospective_evidence_authority_allows_messages_without_chunks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(tmp_path)
    calls: list[dict[str, Any]] = []
    _install_source_ranking(
        monkeypatch,
        service,
        chunk_ids=("chunk_answer",),
        calls=calls,
    )

    result = service.retrieve_retrospective_evidence(
        "question",
        evidence_sources=(
            _source(
                "message-without-chunks",
                available_at="2026-07-01T00:00:00Z",
                chunk_ids=(),
            ),
            _source(
                "answer",
                available_at="2026-07-01T01:00:00Z",
                chunk_ids=("chunk_answer",),
            ),
        ),
        source_available_as_of="2026-07-02T00:00:00Z",
        retrieval_arm="source",
    )

    assert result["ranked_evidence_ids"] == ["answer"]


def test_retrospective_retrieval_requires_read_only_service_without_initializing(
    tmp_path: Path,
) -> None:
    paths = BrainPaths.from_value(tmp_path / "missing-brain")
    service = BrainService(paths)

    with pytest.raises(ReadOnlyModeError, match="read_only=True"):
        service.retrieve_retrospective_evidence(
            "question",
            evidence_sources=(),
            source_available_as_of="2026-07-02T00:00:00Z",
            retrieval_arm="source",
        )

    assert not paths.sqlite_path.exists()
