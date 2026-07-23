from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "run_gmail_temporal_retrieval.py"


def _load() -> Any:
    name = "test_run_gmail_temporal_retrieval"
    spec = importlib.util.spec_from_file_location(name, SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


runner = _load()


def _private_file(path: Path, raw: bytes) -> None:
    path.write_bytes(raw)
    path.chmod(0o600)


def _source(
    source_id: str,
    *,
    available_at: str,
    chunks: tuple[str, ...],
    text: str = "private context",
) -> Any:
    return runner.VerifiedSource(
        source_id=source_id,
        available_at=datetime.fromisoformat(available_at).astimezone(timezone.utc),
        document_id=f"document-{source_id}",
        gmail_message_id=f"message-{source_id}",
        chunk_ids=chunks,
        text=text,
    )


def test_run_blind_queries_excludes_context_and_future_sources() -> None:
    sources = {
        "context": _source(
            "context",
            available_at="2026-07-01T09:00:00+00:00",
            chunks=("context-chunk",),
            text="Bound local context",
        ),
        "answer": _source(
            "answer", available_at="2026-07-01T10:00:00+00:00", chunks=("answer",)
        ),
        "future": _source(
            "future", available_at="2026-07-03T10:00:00+00:00", chunks=("future",)
        ),
    }
    seen: list[tuple[str, str, str, tuple[str, ...]]] = []

    def retrieve(
        query: str, as_of: str, context: str, excluded: tuple[str, ...]
    ) -> tuple[str, ...]:
        seen.append((query, as_of, context, excluded))
        return ("answer",)

    rows = [
        {
            "as_of": "2026-07-02T00:00:00Z",
            "context_source_ids": ["context"],
            "query_id": "query-1",
            "query_text": "What is the current schedule?",
            "version": "blind-query-v1",
        }
    ]

    result = runner.run_blind_queries(
        rows,
        challenge=True,
        sources=sources,
        retriever=retrieve,
    )

    assert seen == [
        (
            "What is the current schedule?",
            "2026-07-02T00:00:00Z",
            "Bound local context",
            ("context",),
        )
    ]
    assert result == [
        {
            "query_id": "query-1",
            "retrieved": [{"rank": 1, "source_id": "answer"}],
            "version": runner.RESULT_VERSION,
        }
    ]


def test_primary_query_can_return_zero_results() -> None:
    rows = [
        {
            "as_of": "2026-07-02T00:00:00Z",
            "query_id": "query-1",
            "query_text": "A cold query\nwith a second line",
            "version": "blind-query-v1",
        }
    ]

    result = runner.run_blind_queries(
        rows,
        challenge=False,
        sources={},
        retriever=lambda _query, _as_of, _context, _excluded: (),
    )

    assert result[0]["retrieved"] == []


def test_challenge_query_requires_context() -> None:
    rows = [
        {
            "as_of": "2026-07-02T00:00:00Z",
            "context_source_ids": [],
            "query_id": "query-1",
            "query_text": "What changed?",
            "version": "blind-query-v1",
        }
    ]

    with pytest.raises(
        runner.GmailTemporalRetrievalRunnerError, match="query context is invalid"
    ):
        runner.run_blind_queries(
            rows,
            challenge=True,
            sources={},
            retriever=lambda _query, _as_of, _context, _excluded: (),
        )


def test_production_adapter_calls_public_read_only_retrieval_api(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Service:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, Any]]] = []

        def retrieve_retrospective_evidence(
            self, query: str, **kwargs: Any
        ) -> dict[str, Any]:
            self.calls.append((query, kwargs))
            return {
                "version": runner.RETROSPECTIVE_RETRIEVAL_VERSION,
                "retrieval_arm": kwargs["retrieval_arm"],
                "source_available_as_of": "2026-07-02T00:00:00+00:00",
                "ranked_evidence_ids": ["answer"],
                "persisted": False,
            }

    monkeypatch.setattr(
        runner,
        "BrainService",
        lambda _paths, read_only: Service(),
    )
    sources = {
        "answer": _source(
            "answer",
            available_at="2026-07-01T09:00:00+00:00",
            chunks=("chunk-answer",),
        )
    }
    retriever = runner.ProductionBrainRetriever(
        runner.BrainPaths.from_value("/tmp/unused"),
        mode="temporal",
        sources=sources,
    )
    result = retriever(
        "what changed?",
        "2026-07-02T00:00:00Z",
        "prior message",
        (),
    )

    assert result == ("answer",)
    assert len(retriever.service.calls) == 1
    query, kwargs = retriever.service.calls[0]
    assert query == "what changed?"
    assert kwargs == {
        "evidence_sources": (
            runner.RetrospectiveEvidenceSource(
                evidence_id="answer",
                available_at="2026-07-01T09:00:00+00:00",
                chunk_ids=("chunk-answer",),
            ),
        ),
        "source_available_as_of": "2026-07-02T00:00:00Z",
        "retrieval_arm": "temporal",
        "excluded_evidence_ids": (),
        "context_text": "prior message",
        "limit": 10,
    }


def test_load_blind_bundle_verifies_hashes_without_opening_gold(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir(mode=0o700)
    sources = runner._jsonl_bytes(  # noqa: SLF001
        [
            {
                "available_at": "2026-07-01T00:00:00Z",
                "source_id": "source-1",
                "version": "source-v1",
            }
        ]
    )
    queries = runner._jsonl_bytes(  # noqa: SLF001
        [
            {
                "as_of": "2026-07-02T00:00:00Z",
                "query_id": "query-1",
                "query_text": "When is the deadline?",
                "version": "query-v1",
            }
        ]
    )
    gold = b"PRIVATE_GOLD_SENTINEL\n"
    artifacts = {
        runner.SOURCE_ARTIFACT: sources,
        runner.PRIMARY_QUERY_ARTIFACT: queries,
        "sealed-primary-gold.jsonl": gold,
    }
    manifest = {
        "artifact_sha256": {
            name: hashlib.sha256(raw).hexdigest() for name, raw in artifacts.items()
        },
        "challenge_query_count": 0,
        "primary_query_count": 1,
        "source_binding_sha256": "c" * 64,
        "source_count": 1,
        "version": "bundle-v-test",
    }
    for name, raw in artifacts.items():
        _private_file(bundle / name, raw)
    _private_file(
        bundle / runner.MANIFEST_ARTIFACT,
        runner._canonical_json(manifest) + b"\n",  # noqa: SLF001
    )

    loaded, source_rows, primary, challenge = runner.load_blind_bundle(bundle)

    assert loaded == manifest
    assert len(source_rows) == 1
    assert len(primary) == 1
    assert challenge == []

    _private_file(bundle / runner.PRIMARY_QUERY_ARTIFACT, queries + b"{}\n")
    with pytest.raises(
        runner.GmailTemporalRetrievalRunnerError,
        match="bundle blind artifact commitment failed",
    ):
        runner.load_blind_bundle(bundle)


def test_source_binding_must_exactly_cover_blind_authority(tmp_path: Path) -> None:
    path = tmp_path / "bindings.jsonl"
    row = {
        "available_at": "2026-07-01T00:00:00Z",
        "chunk_inventory_hmac_sha256": "c" * 64,
        "chunks": [],
        "document_content_sha256": "a" * 64,
        "document_id": "document-1",
        "gmail_account_key": "account-1",
        "gmail_message_id": "message-1",
        "gmail_thread_id": "thread-1",
        "message_sha256": "b" * 64,
        "source_id": "source-1",
        "version": runner.BINDING_VERSION,
    }
    _private_file(path, runner._jsonl_bytes([row]))  # noqa: SLF001
    binding_sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
    authority = [
        {
            "available_at": "2026-07-01T00:00:00Z",
            "source_id": "source-1",
            "version": "source-v1",
        },
        {
            "available_at": "2026-07-01T00:00:00Z",
            "source_id": "source-2",
            "version": "source-v1",
        },
    ]

    with pytest.raises(
        runner.GmailTemporalRetrievalRunnerError,
        match="does not exactly cover blind authority",
    ):
        runner.load_source_bindings(
            path,
            source_rows=authority,
            expected_sha256=binding_sha256,
        )


def test_source_binding_must_match_frozen_manifest_commitment(tmp_path: Path) -> None:
    path = tmp_path / "bindings.jsonl"
    row = {
        "available_at": "2026-07-01T00:00:00Z",
        "chunk_inventory_hmac_sha256": "c" * 64,
        "chunks": [],
        "document_content_sha256": "a" * 64,
        "document_id": "document-1",
        "gmail_account_key": "account-1",
        "gmail_message_id": "message-1",
        "gmail_thread_id": "thread-1",
        "message_sha256": "b" * 64,
        "source_id": "source-1",
        "version": runner.BINDING_VERSION,
    }
    _private_file(path, runner._jsonl_bytes([row]))  # noqa: SLF001

    with pytest.raises(
        runner.GmailTemporalRetrievalRunnerError,
        match="does not match the frozen bundle commitment",
    ):
        runner.load_source_bindings(
            path,
            source_rows=[
                {
                    "available_at": "2026-07-01T00:00:00Z",
                    "source_id": "source-1",
                    "version": "source-v1",
                }
            ],
            expected_sha256="0" * 64,
        )


def _verified_binding_fixture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, ambiguous_chunk: bool = False
) -> tuple[Any, list[dict[str, Any]]]:
    header = "# Email thread: Subject from the later reply"
    first = "## Message 1\n\nFirst message"
    second = "## Message 2\n\nLater reply"
    body = f"{header}\n\n{first}\n\n{second}"
    first_start = len(header) + 2
    first_end = first_start + len(first)
    second_start = first_end + 2
    second_end = second_start + len(second)
    source_path = tmp_path / "thread.md"
    source_path.write_text(body, encoding="utf-8")
    document = {
        "id": "document-1",
        "source_type": "gmail_thread",
        "source_path": str(source_path),
        "raw_path": str(source_path),
        "content_hash": "a" * 64,
        "status": "active",
    }
    timestamps = [
        {
            "message_id": "message-1",
            "internal_date": "2026-07-01T09:00:00Z",
            "start_offset": first_start,
            "end_offset": first_end,
        },
        {
            "message_id": "message-2",
            "internal_date": "2026-07-02T09:00:00Z",
            "start_offset": second_start,
            "end_offset": second_end,
        },
    ]
    chunks = [
        {"chunk_id": "chunk-header", "start_offset": 0, "end_offset": len(header)},
        {
            "chunk_id": "chunk-first",
            "start_offset": first_start,
            # Production markdown chunking retains the separator newlines at
            # the tail of the preceding message section.
            "end_offset": second_start,
        },
        {
            "chunk_id": "chunk-second",
            "start_offset": second_start,
            "end_offset": second_end,
        },
    ]
    if ambiguous_chunk:
        chunks.append(
            {
                "chunk_id": "chunk-crossing",
                "start_offset": first_end - 2,
                "end_offset": second_start + 2,
            }
        )
    monkeypatch.setattr(
        runner, "_active_gmail_document_rows", lambda _paths: {"document-1": document}
    )
    monkeypatch.setattr(
        runner,
        "source_frontmatter_with_path",
        lambda _document: (
            {"gmail_account_key": "account-1", "gmail_thread_id": "thread-1"},
            source_path,
        ),
    )
    monkeypatch.setattr(
        runner,
        "trusted_gmail_message_timestamps",
        lambda _document, _frontmatter, _path: timestamps,
    )
    monkeypatch.setattr(runner, "strip_frontmatter", lambda text: text)
    monkeypatch.setattr(runner, "_document_chunks", lambda _paths, _id: chunks)
    chunk_inventory_by_message = {
        "message-1": [
            {
                "chunk_id": "chunk-first",
                "end_offset": second_start,
                "start_offset": first_start,
                "text_sha256": hashlib.sha256(
                    body[first_start:second_start].encode()
                ).hexdigest(),
            }
        ],
        "message-2": [
            {
                "chunk_id": "chunk-header",
                "end_offset": len(header),
                "start_offset": 0,
                "text_sha256": hashlib.sha256(body[: len(header)].encode()).hexdigest(),
            },
            {
                "chunk_id": "chunk-second",
                "end_offset": second_end,
                "start_offset": second_start,
                "text_sha256": hashlib.sha256(
                    body[second_start:second_end].encode()
                ).hexdigest(),
            },
        ],
    }
    rows = [
        {
            "available_at": timestamp["internal_date"],
            "chunk_inventory_hmac_sha256": "c" * 64,
            "chunks": chunk_inventory_by_message[timestamp["message_id"]],
            "document_content_sha256": "a" * 64,
            "document_id": "document-1",
            "gmail_account_key": "account-1",
            "gmail_message_id": timestamp["message_id"],
            "gmail_thread_id": "thread-1",
            "message_sha256": hashlib.sha256(
                body[timestamp["start_offset"] : timestamp["end_offset"]].encode()
            ).hexdigest(),
            "source_id": f"source-{index}",
            "version": runner.BINDING_VERSION,
        }
        for index, timestamp in enumerate(timestamps, start=1)
    ]
    return runner.BrainPaths.from_value(tmp_path / "brain"), rows


def test_header_chunk_is_clocked_to_latest_trusted_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths, rows = _verified_binding_fixture(tmp_path, monkeypatch)

    sources = runner.verify_source_bindings(paths, rows)

    assert sources["source-1"].chunk_ids == ("chunk-first",)
    assert sources["source-2"].chunk_ids == ("chunk-header", "chunk-second")


def test_binding_thread_must_match_trusted_gmail_frontmatter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths, rows = _verified_binding_fixture(tmp_path, monkeypatch)
    rows[0]["gmail_thread_id"] = "different-thread"

    with pytest.raises(
        runner.GmailTemporalRetrievalRunnerError,
        match="Gmail projection is stale",
    ):
        runner.verify_source_bindings(paths, rows)


def test_chunk_crossing_message_authority_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths, rows = _verified_binding_fixture(tmp_path, monkeypatch, ambiguous_chunk=True)

    with pytest.raises(
        runner.GmailTemporalRetrievalRunnerError,
        match="crosses a message authority boundary",
    ):
        runner.verify_source_bindings(paths, rows)


def test_runtime_rechunking_must_match_authenticated_inventory_exactly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths, rows = _verified_binding_fixture(tmp_path, monkeypatch)
    original_chunks = runner._document_chunks(paths, "document-1")  # noqa: SLF001
    rechunked = [dict(chunk) for chunk in original_chunks]
    rechunked[1]["end_offset"] -= 1
    monkeypatch.setattr(runner, "_document_chunks", lambda _paths, _id: rechunked)

    with pytest.raises(
        runner.GmailTemporalRetrievalRunnerError,
        match="chunk inventory is stale",
    ):
        runner.verify_source_bindings(paths, rows)


def test_runtime_rank_manipulating_chunk_source_reassignment_must_fail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths, rows = _verified_binding_fixture(tmp_path, monkeypatch)
    manipulated = [
        dict(chunk)
        for chunk in runner._document_chunks(paths, "document-1")  # noqa: SLF001
    ]
    first = next(chunk for chunk in manipulated if chunk["chunk_id"] == "chunk-first")
    second = next(chunk for chunk in manipulated if chunk["chunk_id"] == "chunk-second")
    first["start_offset"], second["start_offset"] = (
        second["start_offset"],
        first["start_offset"],
    )
    first["end_offset"], second["end_offset"] = (
        second["end_offset"],
        first["end_offset"],
    )
    monkeypatch.setattr(runner, "_document_chunks", lambda _paths, _id: manipulated)

    with pytest.raises(
        runner.GmailTemporalRetrievalRunnerError,
        match="chunk inventory is stale",
    ):
        runner.verify_source_bindings(paths, rows)


def test_binding_must_cover_every_active_gmail_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths, rows = _verified_binding_fixture(tmp_path, monkeypatch)

    with pytest.raises(
        runner.GmailTemporalRetrievalRunnerError,
        match="does not cover every active Gmail message",
    ):
        runner.verify_source_bindings(paths, rows[:1])


def test_index_snapshot_uses_sqlite_backup_and_is_disposable(tmp_path: Path) -> None:
    source = runner.BrainPaths.from_value(tmp_path / "source")
    source.db_dir.mkdir(parents=True)
    source.config_local.mkdir(parents=True)
    source.lancedb_path.mkdir(parents=True)
    with sqlite3.connect(source.sqlite_path) as conn:
        conn.execute("CREATE TABLE evidence(value TEXT NOT NULL)")
        conn.execute("INSERT INTO evidence VALUES ('original')")
    source.config_file.write_text("embedding:\n  provider: hash\n", encoding="utf-8")
    (source.lancedb_path / "index.bin").write_bytes(b"index")
    destination = runner.BrainPaths.from_value(tmp_path / "destination")

    runner._copy_index_snapshot(source, destination)  # noqa: SLF001
    with sqlite3.connect(destination.sqlite_path) as conn:
        conn.execute("INSERT INTO evidence VALUES ('scratch')")
    with sqlite3.connect(source.sqlite_path) as conn:
        values = [row[0] for row in conn.execute("SELECT value FROM evidence")]

    assert values == ["original"]
    assert destination.config_file.read_text() == source.config_file.read_text()
    assert (destination.lancedb_path / "index.bin").read_bytes() == b"index"


def _execute_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    mutate_snapshot: bool,
) -> tuple[Any, Path, str]:
    paths = runner.BrainPaths.from_value(tmp_path / "brain")
    paths.db_dir.mkdir(parents=True)
    paths.config_local.mkdir(parents=True)
    paths.lancedb_path.mkdir(parents=True)
    with sqlite3.connect(paths.sqlite_path) as conn:
        conn.execute("CREATE TABLE evidence(value TEXT NOT NULL)")
        conn.execute("INSERT INTO evidence VALUES ('live')")
    paths.config_file.write_text("embedding:\n  provider: hash\n", encoding="utf-8")
    (paths.lancedb_path / "index.bin").write_bytes(b"vector index")
    binding_path = tmp_path / "bindings.jsonl"
    binding_raw = b"authenticated source binding\n"
    _private_file(binding_path, binding_raw)
    binding_sha256 = hashlib.sha256(binding_raw).hexdigest()
    manifest = {
        "artifact_sha256": {
            runner.SOURCE_ARTIFACT: "a" * 64,
            runner.PRIMARY_QUERY_ARTIFACT: "b" * 64,
        },
        "source_binding_sha256": binding_sha256,
        "source_count": 0,
    }
    primary = [
        {
            "as_of": "2026-07-02T00:00:00Z",
            "query_id": "query-1",
            "query_text": "When is the deadline?",
            "version": "query-v1",
        }
    ]
    monkeypatch.setattr(
        runner,
        "load_blind_bundle",
        lambda _root: (manifest, [], primary, []),
    )

    def load_bindings(
        _path: Path,
        *,
        source_rows: Any,
        expected_sha256: str,
    ) -> list[dict[str, Any]]:
        assert source_rows == []
        assert expected_sha256 == binding_sha256
        return []

    monkeypatch.setattr(runner, "load_source_bindings", load_bindings)
    monkeypatch.setattr(runner, "verify_source_bindings", lambda _paths, _rows: {})

    class Retriever:
        def __init__(self, scratch_paths: Any, *, mode: str, sources: Any) -> None:
            assert mode == "temporal"
            assert sources == {}
            self.paths = scratch_paths

        def __call__(
            self,
            _query: str,
            _as_of: str,
            _context: str,
            _excluded: tuple[str, ...],
        ) -> tuple[str, ...]:
            if mutate_snapshot:
                with sqlite3.connect(self.paths.sqlite_path) as conn:
                    conn.execute("INSERT INTO evidence VALUES ('scratch mutation')")
            return ()

    monkeypatch.setattr(runner, "ProductionBrainRetriever", Retriever)
    output = tmp_path / "run-output"
    return paths, output, binding_sha256


def test_execute_records_live_and_actual_snapshot_commitments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths, output, binding_sha256 = _execute_fixture(
        tmp_path, monkeypatch, mutate_snapshot=False
    )
    live_before, live_components = runner.index_commitment(paths)

    result = runner.execute_retrieval_run(
        paths.home,
        tmp_path / "blind-bundle",
        tmp_path / "bindings.jsonl",
        output,
        mode="temporal",
    )

    configuration = json.loads((output / runner.CONFIG_ARTIFACT).read_bytes())
    receipt = json.loads((output / runner.INDEX_RECEIPT_ARTIFACT).read_bytes())
    implementation = json.loads((output / runner.IMPLEMENTATION_ARTIFACT).read_bytes())
    live_after, after_components = runner.index_commitment(paths)
    assert live_after == live_before
    assert after_components == live_components
    assert configuration["binding_sha256"] == binding_sha256
    assert (
        configuration["production_retrieval_api"]
        == "BrainService.retrieve_retrospective_evidence"
    )
    assert (
        configuration["production_retrieval_api_version"]
        == runner.RETROSPECTIVE_RETRIEVAL_VERSION
    )
    assert implementation["runner_sha256"] == configuration["runner_sha256"]
    assert implementation["service_sha256"] == configuration["service_sha256"]
    assert (
        implementation["retrospective_retrieval_sha256"]
        == configuration["retrospective_retrieval_sha256"]
    )
    assert (
        hashlib.sha256(
            (output / runner.IMPLEMENTATION_ARTIFACT).read_bytes()
        ).hexdigest()
        == configuration["implementation_provenance_sha256"]
    )
    assert configuration["live_source_index_artifact_sha256"] == live_before
    assert configuration["live_source_index_components"] == live_components
    assert (
        configuration["snapshot_index_artifact_sha256"]
        == receipt["index_artifact_sha256"]
    )
    assert (
        configuration["snapshot_index_components"] == configuration["index_components"]
    )
    assert datetime.fromisoformat(receipt["snapshot_as_of"]).tzinfo is not None
    assert result["live_source_index_unchanged"] is True
    assert result["snapshot_index_unchanged"] is True


def test_execute_rejects_scratch_snapshot_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths, output, _binding_sha256 = _execute_fixture(
        tmp_path, monkeypatch, mutate_snapshot=True
    )

    with pytest.raises(
        runner.GmailTemporalRetrievalRunnerError,
        match="disposable retrieval snapshot changed during execution",
    ):
        runner.execute_retrieval_run(
            paths.home,
            tmp_path / "blind-bundle",
            tmp_path / "bindings.jsonl",
            output,
            mode="temporal",
        )
    assert not output.exists()


def test_private_artifact_rejects_group_readable_file(tmp_path: Path) -> None:
    path = tmp_path / "unsafe.jsonl"
    path.write_text("{}\n", encoding="utf-8")
    os.chmod(path, 0o640)

    with pytest.raises(
        runner.GmailTemporalRetrievalRunnerError, match="not owner-only"
    ):
        runner._private_file(path, label="unsafe")  # noqa: SLF001


def test_private_artifact_rejects_inode_swap_during_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "private.json"
    replacement = tmp_path / "replacement.json"
    _private_file(path, b'{"original":true}\n')
    _private_file(replacement, b'{"replacement":true}\n')
    original_open = os.open

    def racing_open(target: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        if Path(target) == path:
            os.replace(replacement, path)
        return original_open(target, flags, *args, **kwargs)

    monkeypatch.setattr(runner.os, "open", racing_open)

    with pytest.raises(
        runner.GmailTemporalRetrievalRunnerError,
        match="changed while it was being opened",
    ):
        runner._private_file(path, label="private artifact")  # noqa: SLF001
