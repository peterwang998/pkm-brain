from __future__ import annotations

from pathlib import Path

import pytest

from pkm_brain.db import connection
from pkm_brain.gmail_knowledge import (
    GMAIL_KNOWLEDGE_CLASSIFIER_VERSION,
    GMAIL_KNOWLEDGE_PROJECTION_VERSION,
    reconcile_gmail_document_revisions,
)
from pkm_brain.indexes import upsert_vectors, vector_chunk_ids
from pkm_brain.paths import BrainPaths
from pkm_brain.service import BrainService, insert_chunk_retrieval_fts


def service_for(tmp_path: Path) -> BrainService:
    return BrainService(BrainPaths.from_value(tmp_path / "brain"))


def write_note(path: Path, title: str, body: str) -> None:
    path.write_text(f"# {title}\n\n{body}\n", encoding="utf-8")


def write_gmail_revision(
    path: Path,
    *,
    title: str,
    revision: str,
    archive_updated_at: str,
    body: str,
    account_key: str = "gmail.primary",
    projection_version: int | None = None,
    captured_at: str | None = None,
) -> None:
    projection_line = (
        f"gmail_projection_version: {projection_version}\n"
        if projection_version is not None
        else ""
    )
    path.write_text(
        "---\n"
        f'title: "{title}"\n'
        "source_type: gmail_thread\n"
        'source_trust: "untrusted_external"\n'
        f'gmail_account_key: "{account_key}"\n'
        'gmail_thread_id: "thread-1"\n'
        f'gmail_source_revision: "{revision}"\n'
        f"{projection_line}"
        f'archive_updated_at: "{archive_updated_at}"\n'
        f'captured_at: "{captured_at or archive_updated_at}"\n'
        "fact_eligible: true\n"
        "deleted: false\n"
        "---\n\n"
        f"# Email thread: {title}\n\n"
        "## Message 1 — 2026-07-01T16:00:00+00:00 — message-1\n\n"
        f"{body}\n",
        encoding="utf-8",
    )


def document_chunks(svc: BrainService) -> dict[str, set[str]]:
    with connection(svc.paths.sqlite_path) as conn:
        values: dict[str, set[str]] = {}
        for row in conn.execute(
            """
            SELECT d.source_path, c.id
            FROM documents d
            JOIN chunks c ON c.document_id = d.id
            ORDER BY d.source_path, c.chunk_index
            """
        ):
            key = Path(str(row["source_path"])).name
            values.setdefault(key, set()).add(str(row["id"]))
    return values


def test_gmail_ingest_migrates_legacy_node_origin_without_duplicate(
    tmp_path: Path,
) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    gmail_root = svc.paths.inbox / "documents" / "gmail"
    gmail_root.mkdir(parents=True)
    revision = gmail_root / "portable.md"
    write_gmail_revision(
        revision,
        title="Portable Gmail Revision",
        revision="portable-revision",
        archive_updated_at="2026-07-17T16:00:00+00:00",
        body="Portable revision marker.",
        projection_version=GMAIL_KNOWLEDGE_PROJECTION_VERSION,
    )
    first = svc.ingest(source=gmail_root)
    assert first.changed == 1
    with connection(svc.paths.sqlite_path) as conn:
        document_id = str(conn.execute("SELECT id FROM documents").fetchone()[0])
        conn.execute(
            "UPDATE documents SET origin_node_id = 'legacy-host' WHERE id = ?",
            (document_id,),
        )

    second = svc.ingest(source=gmail_root)

    assert second.changed == 0
    assert second.skipped == 1
    with connection(svc.paths.sqlite_path) as conn:
        rows = list(
            conn.execute(
                "SELECT id, origin_node_id FROM documents WHERE source_type = 'gmail_thread'"
            )
        )
    assert [(str(row["id"]), str(row["origin_node_id"])) for row in rows] == [
        (document_id, "gmail-knowledge")
    ]


def test_external_gmail_sources_keep_distinct_explicit_origins(tmp_path: Path) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    for origin in ("node-a", "node-b"):
        revision = (
            svc.paths.inbox
            / "external"
            / origin
            / "documents"
            / "gmail"
            / "portable.md"
        )
        revision.parent.mkdir(parents=True)
        write_gmail_revision(
            revision,
            title="External Gmail Revision",
            revision=f"revision-{origin}",
            archive_updated_at="2026-07-17T16:00:00+00:00",
            body=f"External origin marker {origin}.",
            projection_version=GMAIL_KNOWLEDGE_PROJECTION_VERSION,
        )

    result = svc.ingest(source=svc.paths.inbox / "external")

    assert result.changed == 2
    with connection(svc.paths.sqlite_path) as conn:
        origins = {
            str(row["origin_node_id"])
            for row in conn.execute(
                "SELECT origin_node_id FROM documents WHERE source_type='gmail_thread'"
            )
        }
    assert origins == {"node-a", "node-b"}


def test_vector_rebuild_rechunk_and_chunk_lookup_ignore_inactive_documents(
    tmp_path: Path,
) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    write_note(
        svc.paths.inbox / "active.md",
        "Active Evidence",
        "The active aurora evidence should remain retrievable.",
    )
    write_note(
        svc.paths.inbox / "retired.md",
        "Retired Evidence",
        "The retired quasar evidence must not return to retrieval.",
    )
    svc.ingest()
    chunks = document_chunks(svc)
    active_ids = chunks["active.md"]
    retired_ids = chunks["retired.md"]
    with connection(svc.paths.sqlite_path) as conn:
        conn.execute(
            "UPDATE documents SET status = 'superseded' WHERE source_path LIKE '%/retired.md'"
        )
    fts_ids = {
        str(row["chunk_id"])
        for row in svc._search_fts("evidence", limit=10)
    }
    assert fts_ids == active_ids

    repair = svc.rebuild_vector_index(missing_only=True)
    assert repair["status"] == "ok"
    assert repair["stale_vectors_removed"] == len(retired_ids)
    assert vector_chunk_ids(svc.paths.lancedb_path) == active_ids

    rebuild = svc.rebuild_vector_index(delete_backup=True)

    assert rebuild["status"] == "ok"
    assert rebuild["sqlite_chunks"] == len(active_ids)
    assert vector_chunk_ids(svc.paths.lancedb_path) == active_ids
    assert {
        row["chunk_id"]
        for row in svc._chunks_by_ids([*retired_ids, *active_ids])
    } == active_ids
    rechunk = svc.reindex_chunks(
        source_type="markdown_note", dry_run=True, all_documents=True
    )
    assert rechunk["affected_documents"] == 1
    assert str(rechunk["documents"][0]["source_path"]).endswith("/active.md")
    doctor = svc.index_doctor()
    assert doctor["status"] == "ok"
    assert doctor["sqlite_chunks"] == len(active_ids)
    assert doctor["missing_vector_count"] == 0
    assert doctor["stale_vector_count"] == 0


def test_reconciliation_repurges_already_retired_gmail_revision(tmp_path: Path) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    gmail_root = svc.paths.inbox / "documents" / "gmail"
    gmail_root.mkdir(parents=True)
    write_gmail_revision(
        gmail_root / "old.md",
        title="Old Gmail Revision",
        revision="old-revision",
        archive_updated_at="2026-07-17T16:00:00+00:00",
        body="Old revision marker retained only as immutable fact evidence.",
    )
    write_gmail_revision(
        gmail_root / "new.md",
        title="New Gmail Revision",
        revision="new-revision",
        archive_updated_at="2026-07-17T16:10:00+00:00",
        body="New revision marker remains active for retrieval.",
    )
    svc.ingest(source=gmail_root)
    first = reconcile_gmail_document_revisions(svc.paths)
    assert first.superseded_documents == 1
    chunks = document_chunks(svc)
    old_chunk_ids = chunks["old.md"]

    with connection(svc.paths.sqlite_path) as conn:
        for chunk_id in old_chunk_ids:
            row = conn.execute(
                """
                SELECT c.text, c.heading_path, d.title, d.id AS document_id
                FROM chunks c
                JOIN documents d ON d.id = c.document_id
                WHERE c.id = ?
                """,
                (chunk_id,),
            ).fetchone()
            assert row is not None
            insert_chunk_retrieval_fts(
                conn,
                chunk_id=chunk_id,
                title=row["title"],
                text=row["text"],
                heading_path=row["heading_path"],
                project="",
                tags="",
            )
            upsert_vectors(
                svc.paths.lancedb_path,
                [
                    {
                        "chunk_id": chunk_id,
                        "document_id": row["document_id"],
                        "text": row["text"],
                        "vector": svc.embedding_provider.embed([row["text"]])[0],
                    }
                ],
                svc.embedding_provider,
            )

    second = reconcile_gmail_document_revisions(svc.paths)

    assert second.retrieval_chunks_removed == len(old_chunk_ids)
    with connection(svc.paths.sqlite_path) as conn:
        placeholders = ",".join("?" for _ in old_chunk_ids)
        assert conn.execute(
            f"SELECT COUNT(*) FROM chunk_fts WHERE chunk_id IN ({placeholders})",
            list(old_chunk_ids),
        ).fetchone()[0] == 0
        assert conn.execute(
            f"""
            SELECT COUNT(*)
            FROM retrieval_fts
            WHERE kind = 'chunk' AND target_id IN ({placeholders})
            """,
            list(old_chunk_ids),
        ).fetchone()[0] == 0
        retained = {
            str(row["id"])
            for row in conn.execute(
                f"SELECT id FROM chunks WHERE id IN ({placeholders})",
                list(old_chunk_ids),
            )
        }
    assert retained == old_chunk_ids
    assert old_chunk_ids.isdisjoint(vector_chunk_ids(svc.paths.lancedb_path))
    third = reconcile_gmail_document_revisions(svc.paths)
    assert third.retrieval_chunks_removed == 0
    assert third.vectors_removed == 0


def test_reconciliation_holds_reactivated_fallback_when_vectors_cannot_restore(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    gmail_root = svc.paths.inbox / "documents" / "gmail"
    gmail_root.mkdir(parents=True)
    old_path = gmail_root / "old.md"
    new_path = gmail_root / "new.md"
    write_gmail_revision(
        old_path,
        title="Old Gmail Revision",
        revision="old-revision",
        archive_updated_at="2026-07-17T16:00:00+00:00",
        body="Old revision is the last valid immutable fallback.",
    )
    write_gmail_revision(
        new_path,
        title="New Gmail Revision",
        revision="new-revision",
        archive_updated_at="2026-07-17T16:10:00+00:00",
        body="New revision is initially active for retrieval.",
    )
    svc.ingest(source=gmail_root)
    reconcile_gmail_document_revisions(svc.paths)
    chunks = document_chunks(svc)
    old_chunk_ids = chunks["old.md"]

    with connection(svc.paths.sqlite_path) as conn:
        new_raw_path = Path(
            str(
                conn.execute(
                    "SELECT raw_path FROM documents WHERE source_path=?",
                    (str(new_path),),
                ).fetchone()[0]
            )
        )
    new_path.write_text("invalid current source\n", encoding="utf-8")
    new_raw_path.write_text("invalid current raw\n", encoding="utf-8")
    monkeypatch.setattr(
        BrainService,
        "rebuild_vector_index",
        lambda self, **kwargs: {"status": "skipped"},
    )

    result = reconcile_gmail_document_revisions(svc.paths)

    assert result.reactivated_documents == 0
    assert result.held_documents == 1
    assert result.retrieval_chunks_restored == 0
    assert result.errors == (
        "Gmail reactivated-vector repair did not complete",
    )
    with connection(svc.paths.sqlite_path) as conn:
        statuses = {
            str(row["status"])
            for row in conn.execute(
                "SELECT status FROM documents WHERE source_type='gmail_thread'"
            )
        }
        placeholders = ",".join("?" for _ in old_chunk_ids)
        indexed = conn.execute(
            f"SELECT COUNT(*) FROM chunk_fts WHERE chunk_id IN ({placeholders})",
            list(old_chunk_ids),
        ).fetchone()[0]
    assert statuses == {"superseded"}
    assert indexed == 0
    assert old_chunk_ids.isdisjoint(vector_chunk_ids(svc.paths.lancedb_path))


def test_reconciliation_does_not_retire_same_thread_id_from_another_account(
    tmp_path: Path,
) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    gmail_root = svc.paths.inbox / "documents" / "gmail"
    gmail_root.mkdir(parents=True)
    write_gmail_revision(
        gmail_root / "primary.md",
        title="Primary Account Thread",
        revision="primary-revision",
        archive_updated_at="2026-07-17T16:00:00+00:00",
        body="Primary-account evidence remains independently active.",
        account_key="gmail.primary",
    )
    write_gmail_revision(
        gmail_root / "secondary.md",
        title="Secondary Account Thread",
        revision="secondary-revision",
        archive_updated_at="2026-07-17T16:10:00+00:00",
        body="Secondary-account evidence remains independently active.",
        account_key="gmail.secondary",
    )
    svc.ingest(source=gmail_root)

    result = reconcile_gmail_document_revisions(svc.paths)

    assert result.active_documents == 2
    assert result.superseded_documents == 0


def test_reconciliation_prefers_newest_projection_and_keeps_prior_evidence(
    tmp_path: Path,
) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    gmail_root = svc.paths.inbox / "documents" / "gmail"
    gmail_root.mkdir(parents=True)
    shared_revision = "same-provider-revision"
    shared_archive_time = "2026-07-17T16:00:00+00:00"
    write_gmail_revision(
        gmail_root / "legacy.md",
        title="Legacy Projection",
        revision=shared_revision,
        archive_updated_at=shared_archive_time,
        captured_at="2026-07-17T18:00:00+00:00",
        body="Legacy unversioned evidence remains retained after reconciliation.",
    )
    write_gmail_revision(
        gmail_root / "projection-v1.md",
        title="Projection One",
        revision=shared_revision,
        archive_updated_at=shared_archive_time,
        projection_version=1,
        captured_at="2026-07-17T17:00:00+00:00",
        body="Projection one evidence remains retained after reconciliation.",
    )
    write_gmail_revision(
        gmail_root / "projection-v2.md",
        title="Projection Two",
        revision=shared_revision,
        archive_updated_at=shared_archive_time,
        projection_version=2,
        # The version outranks capture clock differences for the same provider
        # revision, avoiding clock skew from selecting an older renderer.
        captured_at="2026-07-17T16:30:00+00:00",
        body="Projection two is the active deterministic retrieval projection.",
    )
    svc.ingest(source=gmail_root)

    first = reconcile_gmail_document_revisions(svc.paths)
    second = reconcile_gmail_document_revisions(svc.paths)

    assert first.active_documents == 1
    assert first.superseded_documents == 2
    assert second.active_documents == 1
    assert second.superseded_documents == 2
    with connection(svc.paths.sqlite_path) as conn:
        statuses = dict(
            conn.execute(
                "SELECT title, status FROM documents WHERE source_type = 'gmail_thread'"
            )
        )
        evidence_count = conn.execute(
            """
            SELECT COUNT(*)
            FROM chunks c
            JOIN documents d ON d.id = c.document_id
            WHERE d.source_type = 'gmail_thread'
            """
        ).fetchone()[0]
        indexed_count = conn.execute(
            """
            SELECT COUNT(*)
            FROM chunk_fts f
            JOIN chunks c ON c.id = f.chunk_id
            JOIN documents d ON d.id = c.document_id
            WHERE d.source_type = 'gmail_thread'
            """
        ).fetchone()[0]
    assert statuses == {
        "Legacy Projection": "superseded",
        "Projection One": "superseded",
        "Projection Two": "active",
    }
    assert evidence_count > indexed_count > 0


def test_reconciliation_fails_closed_for_invalid_gmail_lineage(tmp_path: Path) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    gmail_root = svc.paths.inbox / "documents" / "gmail"
    gmail_root.mkdir(parents=True)
    malformed = gmail_root / "malformed.md"
    malformed.write_text(
        "---\ntitle: Malformed Gmail projection\nsource_type: gmail_thread\n---\n"
        "This document lacks connector-authored account, thread, and revision lineage.\n",
        encoding="utf-8",
    )
    svc.ingest(source=gmail_root)

    result = reconcile_gmail_document_revisions(svc.paths)

    with connection(svc.paths.sqlite_path) as conn:
        status = conn.execute(
            "SELECT status FROM documents WHERE source_path = ?", (str(malformed),)
        ).fetchone()[0]
        indexed = conn.execute(
            """
            SELECT COUNT(*)
            FROM chunk_fts f
            JOIN chunks c ON c.id=f.chunk_id
            JOIN documents d ON d.id=c.document_id
            WHERE d.source_path=?
            """,
            (str(malformed),),
        ).fetchone()[0]
    assert status == "superseded"
    assert indexed == 0
    assert result.superseded_documents == 1


def test_reconciliation_rejects_nonempty_but_invalid_current_lineage(
    tmp_path: Path,
) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    gmail_root = svc.paths.inbox / "documents" / "gmail"
    gmail_root.mkdir(parents=True)
    malformed = gmail_root / "malformed-current.md"
    write_gmail_revision(
        malformed,
        title="Malformed Current Projection",
        revision="not-a-provider-revision-digest",
        archive_updated_at="2026-07-17T16:00:00+00:00",
        projection_version=GMAIL_KNOWLEDGE_PROJECTION_VERSION,
        body="All lineage fields are present, but they are not trustworthy.",
    )
    original = malformed.read_text(encoding="utf-8")
    malformed.write_text(
        original.replace(
            "gmail_projection_version: "
            f"{GMAIL_KNOWLEDGE_PROJECTION_VERSION}\n",
            "gmail_projection_version: "
            f"{GMAIL_KNOWLEDGE_PROJECTION_VERSION}\n"
            f"gmail_classifier_version: {GMAIL_KNOWLEDGE_CLASSIFIER_VERSION}\n",
        ),
        encoding="utf-8",
    )
    svc.ingest(source=gmail_root)

    result = reconcile_gmail_document_revisions(svc.paths)

    with connection(svc.paths.sqlite_path) as conn:
        status = conn.execute(
            "SELECT status FROM documents WHERE source_path = ?", (str(malformed),)
        ).fetchone()[0]
        indexed = conn.execute(
            """
            SELECT COUNT(*)
            FROM chunk_fts f
            JOIN chunks c ON c.id=f.chunk_id
            JOIN documents d ON d.id=c.document_id
            WHERE d.source_path=?
            """,
            (str(malformed),),
        ).fetchone()[0]
    assert status == "superseded"
    assert indexed == 0
    assert result.superseded_documents == 1


def test_reset_retrieval_index_preserves_inactive_evidence_chunks(
    tmp_path: Path,
) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    write_note(
        svc.paths.inbox / "active.md",
        "Active Reset",
        "Active reset marker is rebuilt into the retrieval indexes.",
    )
    write_note(
        svc.paths.inbox / "retired.md",
        "Retired Reset",
        "Retired reset marker remains only as immutable evidence.",
    )
    svc.ingest()
    before = document_chunks(svc)
    active_before = before["active.md"]
    retired_before = before["retired.md"]
    with connection(svc.paths.sqlite_path) as conn:
        conn.execute(
            "UPDATE documents SET status = 'superseded' WHERE source_path LIKE '%/retired.md'"
        )

    result = svc.reset_retrieval_index()

    assert result["status"] == "ok"
    assert result["chunks_deleted"] == len(active_before)
    after = document_chunks(svc)
    assert after["retired.md"] == retired_before
    assert after["active.md"]
    assert after["active.md"].isdisjoint(active_before)
    with connection(svc.paths.sqlite_path) as conn:
        indexed_ids = {
            str(row["chunk_id"])
            for row in conn.execute("SELECT chunk_id FROM chunk_fts")
        }
    assert indexed_ids == after["active.md"]
    assert vector_chunk_ids(svc.paths.lancedb_path) == after["active.md"]
    assert svc.index_doctor()["status"] == "ok"
