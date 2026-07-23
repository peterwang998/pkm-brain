from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "build_gmail_temporal_retrieval_source_authority.py"


def _load() -> Any:
    name = "test_build_gmail_temporal_retrieval_source_authority"
    spec = importlib.util.spec_from_file_location(name, SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


builder = _load()


def _private_file(path: Path, raw: bytes) -> None:
    path.write_bytes(raw)
    path.chmod(0o600)


def test_chunk_assignment_maps_title_to_latest_message() -> None:
    messages = [
        {
            "message_id": "message-1",
            "start_offset": 10,
            "end_offset": 20,
        },
        {
            "message_id": "message-2",
            "start_offset": 22,
            "end_offset": 32,
        },
    ]

    result = builder._chunk_message_assignments(  # noqa: SLF001
        [
            {"chunk_id": "header", "start_offset": 0, "end_offset": 8},
            {"chunk_id": "first", "start_offset": 10, "end_offset": 22},
            {"chunk_id": "second", "start_offset": 22, "end_offset": 32},
        ],
        messages,
    )

    assert result == {
        "header": "message-2",
        "first": "message-1",
        "second": "message-2",
    }


def test_chunk_assignment_rejects_cross_message_chunk() -> None:
    with pytest.raises(
        builder.GmailTemporalRetrievalSourceAuthorityError,
        match="exactly one trusted message",
    ):
        builder._chunk_message_assignments(  # noqa: SLF001
            [{"chunk_id": "crossing", "start_offset": 10, "end_offset": 32}],
            [
                {
                    "message_id": "message-1",
                    "start_offset": 10,
                    "end_offset": 20,
                },
                {
                    "message_id": "message-2",
                    "start_offset": 22,
                    "end_offset": 32,
                },
            ],
        )


def test_chunk_assignment_rejects_header_message_boundary_crossing() -> None:
    with pytest.raises(
        builder.GmailTemporalRetrievalSourceAuthorityError,
        match="exactly one trusted message",
    ):
        builder._chunk_message_assignments(  # noqa: SLF001
            [{"chunk_id": "crossing", "start_offset": 0, "end_offset": 15}],
            [
                {
                    "message_id": "message-1",
                    "start_offset": 10,
                    "end_offset": 20,
                }
            ],
        )


def test_derive_authority_covers_every_trusted_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = "TITLE\n\nMESSAGE-ONE\n\nMESSAGE-TWO"
    projection = tmp_path / "gmail.md"
    projection.write_text(f"---\nx: y\n---\n\n{body}\n", encoding="utf-8")
    messages = [
        {
            "message_id": "message-1",
            "internal_date": "2026-07-01T09:00:00Z",
            "start_offset": body.index("MESSAGE-ONE"),
            "end_offset": body.index("MESSAGE-ONE") + len("MESSAGE-ONE"),
        },
        {
            "message_id": "message-2",
            "internal_date": "2026-07-02T09:00:00Z",
            "start_offset": body.index("MESSAGE-TWO"),
            "end_offset": body.index("MESSAGE-TWO") + len("MESSAGE-TWO"),
        },
    ]
    document = {
        "id": "document-1",
        "source_type": "gmail_thread",
        "source_path": str(projection),
        "raw_path": str(projection),
        "content_hash": "a" * 64,
        "status": "active",
    }
    monkeypatch.setattr(builder, "_active_gmail_documents", lambda _paths: [document])
    monkeypatch.setattr(
        builder,
        "source_frontmatter_with_path",
        lambda _document: (
            {"gmail_account_key": "account-1", "gmail_thread_id": "thread-1"},
            projection,
        ),
    )
    monkeypatch.setattr(
        builder,
        "trusted_gmail_message_timestamps",
        lambda _document, _frontmatter, _path: messages,
    )
    monkeypatch.setattr(
        builder,
        "_document_chunks",
        lambda _paths, _document_id: [
            {
                "chunk_id": "header",
                "start_offset": 0,
                "end_offset": body.index("MESSAGE-ONE") - 2,
            },
            {
                "chunk_id": "message-one",
                "start_offset": messages[0]["start_offset"],
                "end_offset": messages[0]["end_offset"],
            },
            {
                "chunk_id": "message-two",
                "start_offset": messages[1]["start_offset"],
                "end_offset": messages[1]["end_offset"],
            },
        ],
    )

    sources, bindings, summary = builder.derive_source_authority(
        builder.BrainPaths.from_value(tmp_path / "brain"), key=b"k" * 32
    )

    assert summary == {"document_count": 1, "message_count": 2, "chunk_count": 3}
    assert len(sources) == len(bindings) == 2
    assert {row["source_id"] for row in sources} == {
        row["source_id"] for row in bindings
    }
    assert {row["available_at"] for row in sources} == {
        "2026-07-01T09:00:00+00:00",
        "2026-07-02T09:00:00+00:00",
    }
    assert all(row["source_id"].startswith("gtrs_") for row in sources)
    assert len({row["thread_scope_id"] for row in sources}) == 1
    assert {row["gmail_message_id"] for row in bindings} == {
        "message-1",
        "message-2",
    }
    assert {row["gmail_thread_id"] for row in bindings} == {"thread-1"}
    binding_by_message = {row["gmail_message_id"]: row for row in bindings}
    assert [
        chunk["chunk_id"] for chunk in binding_by_message["message-1"]["chunks"]
    ] == ["message-one"]
    assert [
        chunk["chunk_id"] for chunk in binding_by_message["message-2"]["chunks"]
    ] == ["header", "message-two"]
    assert all(len(row["chunk_inventory_hmac_sha256"]) == 64 for row in bindings)


def test_derive_authority_includes_unindexed_message_in_recall_denominator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    projection = tmp_path / "gmail.md"
    projection.write_text("---\nx: y\n---\n\nONE\n\nTWO\n", encoding="utf-8")
    document = {
        "id": "document-1",
        "source_type": "gmail_thread",
        "content_hash": "a" * 64,
        "status": "active",
    }
    messages = [
        {
            "message_id": "one",
            "internal_date": "2026-07-01T00:00:00Z",
            "start_offset": 0,
            "end_offset": 3,
        },
        {
            "message_id": "two",
            "internal_date": "2026-07-02T00:00:00Z",
            "start_offset": 5,
            "end_offset": 8,
        },
    ]
    monkeypatch.setattr(builder, "_active_gmail_documents", lambda _paths: [document])
    monkeypatch.setattr(
        builder,
        "source_frontmatter_with_path",
        lambda _document: (
            {"gmail_account_key": "account", "gmail_thread_id": "thread"},
            projection,
        ),
    )
    monkeypatch.setattr(
        builder,
        "trusted_gmail_message_timestamps",
        lambda _document, _frontmatter, _path: messages,
    )
    monkeypatch.setattr(
        builder,
        "_document_chunks",
        lambda _paths, _document_id: [
            {"chunk_id": "one", "start_offset": 0, "end_offset": 3}
        ],
    )

    sources, bindings, summary = builder.derive_source_authority(
        builder.BrainPaths.from_value(tmp_path / "brain"), key=b"k" * 32
    )

    assert len(sources) == len(bindings) == 2
    assert summary == {"document_count": 1, "message_count": 2, "chunk_count": 1}


def test_build_publishes_owner_only_hash_bound_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_rows = [
        {
            "available_at": "2026-07-01T00:00:00+00:00",
            "source_id": "gtrs_source",
            "thread_scope_id": "gtrt_thread",
            "version": builder.SOURCE_VERSION,
        }
    ]
    binding_rows = [
        {
            "available_at": "2026-07-01T00:00:00+00:00",
            "chunk_inventory_hmac_sha256": "c" * 64,
            "chunks": [
                {
                    "chunk_id": "chunk-private",
                    "end_offset": 10,
                    "start_offset": 0,
                    "text_sha256": "d" * 64,
                }
            ],
            "document_content_sha256": "a" * 64,
            "document_id": "document-private",
            "gmail_account_key": "account-private",
            "gmail_message_id": "message-private",
            "gmail_thread_id": "thread-private",
            "message_sha256": "b" * 64,
            "source_id": "gtrs_source",
            "version": builder.BINDING_VERSION,
        }
    ]
    monkeypatch.setattr(
        builder,
        "derive_source_authority",
        lambda _paths, key: (
            source_rows,
            binding_rows,
            {"document_count": 1, "message_count": 1, "chunk_count": 1},
        ),
    )
    key_path = tmp_path / "key"
    _private_file(key_path, b"secret" * 8)
    output = tmp_path / "authority"

    result = builder.build_source_authority(tmp_path / "brain", key_path, output)

    assert result["message_count"] == 1
    assert stat_mode(output) == 0o700
    assert all(stat_mode(path) == 0o600 for path in output.iterdir())
    manifest = json.loads((output / builder.MANIFEST_ARTIFACT).read_text())
    source_raw = (output / builder.SOURCE_ARTIFACT).read_bytes()
    binding_raw = (output / builder.BINDING_ARTIFACT).read_bytes()
    assert manifest["artifact_sha256"] == {
        builder.SOURCE_ARTIFACT: builder._sha256_bytes(source_raw),  # noqa: SLF001
        builder.BINDING_ARTIFACT: builder._sha256_bytes(binding_raw),  # noqa: SLF001
    }
    assert b"account-private" not in source_raw
    assert b"message-private" not in source_raw
    assert b"account-private" in binding_raw


def stat_mode(path: Path) -> int:
    return os.stat(path).st_mode & 0o777
