from __future__ import annotations

from pathlib import Path

from pkm_brain.db import connection
from pkm_brain.paths import BrainPaths
from pkm_brain.service import BrainService
from pkm_brain.source_dates import (
    document_source_date_metadata,
    stamp_candidate_source_context,
)
from pkm_brain.ui_server import enrich_fact_like


def test_queue_fact_source_date_prefers_source_event_over_processing_time(
    tmp_path: Path,
) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()
    source_path = paths.raw / "hightouch.md"
    source_path.write_text(
        "---\n"
        'captured_at: "2026-05-11T22:27:39+00:00"\n'
        'created_at: "2026-04-22T15:57:37.449Z"\n'
        'event_started_at: "2026-04-22T16:00:00+00:00"\n'
        "---\n\n# Hightouch\n",
        encoding="utf-8",
    )
    with connection(paths.sqlite_path) as conn:
        conn.execute(
            """
            INSERT INTO documents(
              id, source_type, title, source_path, raw_path, content_hash,
              created_at, ingested_at, tags, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "doc_hightouch",
                "hyprnote_meeting",
                "Hightouch Interview",
                str(source_path),
                str(source_path),
                "hash-hightouch",
                "2026-07-13T07:25:16+00:00",
                "2026-07-13T07:25:16+00:00",
                "[]",
                "active",
            ),
        )
        conn.execute(
            """
            INSERT INTO chunks(
              id, document_id, chunk_index, text, token_count, content_hash,
              created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "chunk_hightouch",
                "doc_hightouch",
                0,
                "Hightouch source evidence.",
                3,
                "chunk-hightouch",
                "2026-07-13T07:25:16+00:00",
            ),
        )

    enriched = enrich_fact_like(
        paths,
        {
            "id": "fact_hightouch",
            "statement": "Hightouch source evidence.",
            "source_ids": ["chunk:chunk_hightouch"],
            "observed_at": "2026-07-13T07:25:16+00:00",
        },
    )

    assert enriched["source_date"] == "2026-04-22T16:00:00+00:00"
    assert enriched["source_date_basis"] == "source_event_started_at"


def test_extraction_uses_source_date_when_model_omits_observed_at(
    tmp_path: Path,
) -> None:
    source_path = tmp_path / "source.md"
    source_path.write_text(
        "---\ncreated_at: 2026-06-18T09:30:00+00:00\n---\n\n# Source\n",
        encoding="utf-8",
    )
    document = {
        "document_id": "doc_source",
        "source_path": str(source_path),
        "raw_path": str(source_path),
        "created_at": "2026-07-13T08:00:00+00:00",
        "ingested_at": "2026-07-13T08:00:00+00:00",
    }
    document.update(document_source_date_metadata(document))
    candidate = {"statement": "A durable source-backed claim.", "metadata": {}}

    stamp_candidate_source_context(candidate, document, "window-1")

    assert candidate["observed_at"] == "2026-06-18T09:30:00+00:00"
    assert candidate["metadata"]["observed_at_basis"] == "source_created_at"


def test_extraction_does_not_use_document_or_ingest_time_as_assertion_time() -> None:
    document = {
        "document_id": "doc_processing_only",
        "created_at": "2026-07-13T08:00:00+00:00",
        "ingested_at": "2026-07-13T08:01:00+00:00",
    }
    document.update(document_source_date_metadata(document))
    candidate = {"statement": "A durable source-backed claim.", "metadata": {}}

    stamp_candidate_source_context(candidate, document, "window-1")

    assert document["source_date"] == "2026-07-13T08:00:00+00:00"
    assert candidate["observed_at"] is None
    assert candidate["metadata"]["observed_at_basis"] == "unknown"
