from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

from .temporal import TemporalRetrievalRequest


RETROSPECTIVE_RETRIEVAL_ARMS = {"source", "temporal"}
RETROSPECTIVE_RETRIEVAL_VERSION = "retrospective_evidence_retrieval_v1"
RETROSPECTIVE_RETRIEVAL_CHUNK_LIMIT = 60
RETROSPECTIVE_RETRIEVAL_FACT_LIMIT = 32
RETROSPECTIVE_RETRIEVAL_MAX_RESULTS = 10
RETROSPECTIVE_RETRIEVAL_MAX_QUERY_CHARS = 4_000
RETROSPECTIVE_RETRIEVAL_MAX_CONTEXT_CHARS = 24_000


class ReadOnlyModeError(RuntimeError):
    pass


@dataclass(frozen=True)
class RetrospectiveEvidenceSource:
    """One externally verified evidence identity and its indexed chunk coverage."""

    evidence_id: str
    available_at: str
    chunk_ids: tuple[str, ...]


@dataclass(frozen=True)
class _RetrospectiveEvidenceBinding:
    available_at: datetime
    chunk_ids: tuple[str, ...]


class RetrospectiveRetrievalMixin:
    def retrieve_retrospective_evidence(
        self,
        query: str,
        *,
        evidence_sources: tuple[RetrospectiveEvidenceSource, ...],
        source_available_as_of: str,
        retrieval_arm: Literal["source", "temporal"] = "temporal",
        excluded_evidence_ids: tuple[str, ...] = (),
        context_text: str = "",
        limit: int = RETROSPECTIVE_RETRIEVAL_MAX_RESULTS,
    ) -> dict[str, Any]:
        """Return deterministic ranked evidence IDs for source-time replay.

        ``source_available_as_of`` is an external source-availability cutoff,
        never Brain's knowledge clock. Fact retrieval resolves temporal intent
        only from the query, then every fact rank signal is admitted only when
        all of its citations map to the supplied evidence authority without a
        future source. Source ranking disables recency and exposure lineage.

        This surface records no retrieval event or exposure lineage and never
        persists its result.
        """

        if not self.read_only:
            raise ReadOnlyModeError(
                "retrospective retrieval requires BrainService(read_only=True)"
            )
        self._ensure_workspace()
        if retrieval_arm not in RETROSPECTIVE_RETRIEVAL_ARMS:
            raise ValueError("retrieval_arm must be source or temporal")
        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or not (1 <= limit <= RETROSPECTIVE_RETRIEVAL_MAX_RESULTS)
        ):
            raise ValueError(
                f"limit must be between 1 and {RETROSPECTIVE_RETRIEVAL_MAX_RESULTS}"
            )
        replay_query = _retrospective_retrieval_query(query, context_text)
        cutoff, normalized_cutoff = _retrospective_source_timestamp(
            source_available_as_of,
            label="source_available_as_of",
        )
        authority = _retrospective_evidence_authority(evidence_sources)
        excluded = _retrospective_excluded_evidence_ids(
            excluded_evidence_ids,
            authority=authority,
        )

        fact_chunk_ids: list[tuple[str, ...]] = []
        if retrieval_arm == "temporal":
            # Retrospective import time is not knowledge time. In particular,
            # do not pass the source cutoff as ``known_as_of``. Context is
            # untrusted evidence text: it may contain dates or lifecycle
            # language, but only the user's question can select a clock.
            temporal = TemporalRetrievalRequest.resolve(query)
            for fact in self.search_facts(
                replay_query,
                limit=RETROSPECTIVE_RETRIEVAL_FACT_LIMIT,
                temporal_request=temporal,
            ):
                if not isinstance(fact, Mapping):
                    continue
                citations = _complete_fact_citation_chunk_ids(fact)
                if citations is not None:
                    fact_chunk_ids.append(citations)

        chunk_candidates, fanout_debug = self._fanout_chunk_candidates(
            replay_query,
            limit=RETROSPECTIVE_RETRIEVAL_CHUNK_LIMIT,
        )
        rerank_chunks, select_search_results = (
            self._retrospective_ranking_dependencies()
        )
        reranked = rerank_chunks(
            replay_query,
            chunk_candidates,
            fanout_debug,
            lineage_scores={},
            apply_recency=False,
        )
        source_chunk_ids = tuple(
            str(row.get("chunk_id") or "")
            for row in select_search_results(
                reranked,
                limit=RETROSPECTIVE_RETRIEVAL_CHUNK_LIMIT,
            )
            if str(row.get("chunk_id") or "")
        )
        ranked = _rank_retrospective_evidence_ids(
            fact_chunk_ids=tuple(fact_chunk_ids),
            source_chunk_ids=source_chunk_ids,
            authority=authority,
            cutoff=cutoff,
            excluded_evidence_ids=excluded,
            limit=limit,
        )
        return {
            "version": RETROSPECTIVE_RETRIEVAL_VERSION,
            "retrieval_arm": retrieval_arm,
            "source_available_as_of": normalized_cutoff,
            "ranked_evidence_ids": list(ranked),
            "persisted": False,
        }


def _retrospective_retrieval_query(query: str, context_text: str) -> str:
    if (
        not isinstance(query, str)
        or not query.strip()
        or len(query) > RETROSPECTIVE_RETRIEVAL_MAX_QUERY_CHARS
        or "\x00" in query
    ):
        raise ValueError("query is invalid")
    if not isinstance(context_text, str) or "\x00" in context_text:
        raise ValueError("context_text is invalid")
    context = context_text[:RETROSPECTIVE_RETRIEVAL_MAX_CONTEXT_CHARS]
    if not context:
        return query
    return f"Context:\n{context}\n\nFollow-up question:\n{query}"


def _retrospective_source_timestamp(
    value: str,
    *,
    label: str,
) -> tuple[datetime, str]:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{label} is invalid") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{label} must include a timezone")
    normalized = parsed.astimezone(timezone.utc)
    return normalized, normalized.isoformat()


def _retrospective_identifier(value: object, *, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > 256
        or any(character in value for character in "\x00\r\n")
    ):
        raise ValueError(f"{label} is invalid")
    return value


def _retrospective_evidence_authority(
    sources: tuple[RetrospectiveEvidenceSource, ...],
) -> dict[str, _RetrospectiveEvidenceBinding]:
    if not isinstance(sources, tuple) or any(
        not isinstance(source, RetrospectiveEvidenceSource) for source in sources
    ):
        raise ValueError("evidence_sources are invalid")
    authority: dict[str, _RetrospectiveEvidenceBinding] = {}
    for source in sources:
        evidence_id = _retrospective_identifier(
            source.evidence_id,
            label="evidence_id",
        )
        if evidence_id in authority:
            raise ValueError("evidence_sources contain duplicate evidence_id values")
        available_at, _normalized = _retrospective_source_timestamp(
            source.available_at,
            label="evidence available_at",
        )
        if not isinstance(source.chunk_ids, tuple):
            raise ValueError("evidence chunk_ids are invalid")
        try:
            chunk_ids = tuple(
                _retrospective_identifier(chunk_id, label="evidence chunk_id")
                for chunk_id in source.chunk_ids
            )
        except ValueError as exc:
            raise ValueError("evidence chunk_ids are invalid") from exc
        if len(chunk_ids) != len(set(chunk_ids)):
            raise ValueError("evidence chunk_ids are invalid")
        authority[evidence_id] = _RetrospectiveEvidenceBinding(
            available_at=available_at,
            chunk_ids=chunk_ids,
        )
    return authority


def _retrospective_excluded_evidence_ids(
    values: tuple[str, ...],
    *,
    authority: Mapping[str, _RetrospectiveEvidenceBinding],
) -> set[str]:
    if not isinstance(values, tuple):
        raise ValueError("excluded_evidence_ids are invalid")
    normalized = tuple(
        _retrospective_identifier(value, label="excluded evidence_id")
        for value in values
    )
    if len(normalized) != len(set(normalized)):
        raise ValueError("excluded_evidence_ids contain duplicates")
    unknown = set(normalized) - set(authority)
    if unknown:
        raise ValueError("excluded_evidence_ids are outside evidence authority")
    return set(normalized)


def _complete_fact_citation_chunk_ids(
    fact: Mapping[str, Any],
) -> tuple[str, ...] | None:
    """Resolve every declared fact citation to a chunk or reject the whole fact."""

    output: list[str] = []
    spans = fact.get("source_spans", [])
    if spans is None:
        spans = []
    if not isinstance(spans, list):
        return None
    for span in spans:
        if not isinstance(span, Mapping):
            return None
        chunk_id = span.get("chunk_id")
        try:
            value = _retrospective_identifier(chunk_id, label="fact citation chunk_id")
        except ValueError:
            return None
        if value not in output:
            output.append(value)

    source_ids = fact.get("source_ids", [])
    if source_ids is None:
        source_ids = []
    if not isinstance(source_ids, list):
        return None
    for source_id in source_ids:
        if (
            not isinstance(source_id, str)
            or not source_id
            or source_id != source_id.strip()
        ):
            return None
        value = source_id
        if value.startswith("chunk:"):
            value = value.removeprefix("chunk:")
        if not value.startswith("chunk_"):
            return None
        try:
            value = _retrospective_identifier(value, label="fact source chunk_id")
        except ValueError:
            return None
        if value not in output:
            output.append(value)
    return tuple(output) or None


def _rank_retrospective_evidence_ids(
    *,
    fact_chunk_ids: tuple[tuple[str, ...], ...],
    source_chunk_ids: tuple[str, ...],
    authority: Mapping[str, _RetrospectiveEvidenceBinding],
    cutoff: datetime,
    excluded_evidence_ids: set[str],
    limit: int,
) -> tuple[str, ...]:
    all_by_chunk: dict[str, list[str]] = {}
    for evidence_id, binding in authority.items():
        for chunk_id in binding.chunk_ids:
            all_by_chunk.setdefault(chunk_id, []).append(evidence_id)
    for evidence_ids in all_by_chunk.values():
        evidence_ids.sort(
            key=lambda evidence_id: (
                authority[evidence_id].available_at,
                evidence_id,
            )
        )

    def chunk_is_cutoff_safe(chunk_id: str) -> bool:
        evidence_ids = all_by_chunk.get(chunk_id)
        return bool(evidence_ids) and all(
            authority[evidence_id].available_at <= cutoff
            for evidence_id in evidence_ids
        )

    def chunk_is_selectable(chunk_id: str) -> bool:
        evidence_ids = all_by_chunk.get(chunk_id)
        return (
            chunk_is_cutoff_safe(chunk_id)
            and bool(evidence_ids)
            and all(
                evidence_id not in excluded_evidence_ids for evidence_id in evidence_ids
            )
        )

    output: list[str] = []

    def add_chunks(chunk_ids: tuple[str, ...]) -> bool:
        for chunk_id in chunk_ids:
            if not chunk_is_selectable(chunk_id):
                continue
            for evidence_id in all_by_chunk.get(chunk_id, []):
                if evidence_id not in output:
                    output.append(evidence_id)
                    if len(output) >= limit:
                        return True
        return False

    for citations in fact_chunk_ids:
        # A current fact may accumulate a source union after the replay cutoff.
        # Any missing/future citation removes the whole fact's rank signal.
        if not citations or any(
            not chunk_is_cutoff_safe(chunk_id) for chunk_id in citations
        ):
            continue
        if add_chunks(citations):
            return tuple(output)
    add_chunks(source_chunk_ids)
    return tuple(output[:limit])
