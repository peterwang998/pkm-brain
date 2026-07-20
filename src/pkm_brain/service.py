from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

from .audit import valid_memory_scope
from .chunking import (
    DEFAULT_OVERLAP_TOKENS,
    DEFAULT_TARGET_TOKENS,
    chunk_text,
    prepare_text_for_indexing,
    sanitize_agent_session_log,
)
from .db import connection, dumps, init_db, loads, rows
from .embeddings import (
    EmbeddingProviderUnavailable,
    HASH_PROVIDER,
    load_embedding_config,
    passage_embedding_text,
    resolve_embedding_provider,
)
from .fact_records import fact_citation_snapshots, slim_fact_for_context
from .fact_retrieval import search_temporal_facts
from .gmail_retrieval_policy import (
    gmail_document_tags,
    gmail_retrieval_noise_reasons,
    secure_gmail_raw_directories,
)
from .indexes import (
    VectorIndexUnavailable,
    delete_vectors,
    embedding_stamp_report,
    lancedb_stats,
    optimize_vectors,
    path_size,
    search_vectors,
    should_optimize_vectors,
    upsert_vectors,
    vector_chunk_ids,
)
from .paths import BrainPaths, local_node_id
from .temporal import TemporalRetrievalRequest
from .title_utils import bounded_document_title
from .util import file_sha256, new_id, now_iso, slugify, token_count as estimate_tokens


@dataclass
class IngestResult:
    run_id: str
    discovered: int
    changed: int
    skipped: int
    chunks_created: int
    embeddings_created: int
    errors: list[str]
    documents_replaced: int = 0
    vector_writes: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


def chunk_row_id(row: Any) -> str:
    keys = row.keys()
    if "id" in keys:
        return str(row["id"])
    return str(row["chunk_id"])


def automation_run_summary(row: Any | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "id": row["id"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "status": row["status"],
        "error": row["error"],
    }


def parse_doctor_timestamp(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


GENERIC_CONTEXT_TERMS = {
    "about",
    "answer",
    "based",
    "brain",
    "context",
    "current",
    "detail",
    "details",
    "evidence",
    "explain",
    "fetch",
    "from",
    "give",
    "idea",
    "ideas",
    "include",
    "includes",
    "including",
    "know",
    "knows",
    "local",
    "memory",
    "open",
    "only",
    "people",
    "project",
    "projects",
    "plan",
    "plans",
    "question",
    "questions",
    "retrieve",
    "show",
    "state",
    "status",
    "summarize",
    "tell",
    "test",
    "tests",
    "them",
    "topic",
    "topics",
    "use",
    "using",
    "what",
}

GENERIC_BUSINESS_TERMS = {
    "business",
    "commercial",
    "customer",
    "customers",
    "enterprise",
    "enterprises",
    "market",
    "regulatory",
    "value",
}

BROAD_RETRIEVAL_TERMS = (
    GENERIC_CONTEXT_TERMS
    | GENERIC_BUSINESS_TERMS
    | {
        "access",
        "architecture",
        "decision",
        "decisions",
        "deployment",
        "engineering",
        "hardware",
        "maintenance",
        "model",
        "network",
        "personal",
        "pipeline",
        "pipelines",
        "rollout",
        "sensor",
        "status",
        "timeline",
        "user",
        "users",
        "vendor",
        "vendors",
        "venture",
    }
)

AGENT_QUERY_TERMS = {
    "agent",
    "agents",
    "claude",
    "codex",
    "command",
    "commands",
    "implementation",
    "log",
    "logs",
    "mcp",
    "opencode",
    "session",
    "sessions",
    "tool",
    "tools",
}

SOURCE_TYPE_WEIGHTS = {
    "hyprnote_meeting": 4.0,
    "gmail_thread": 0.0,
    "markdown_note": 3.0,
    "meeting_transcript": 2.0,
    "web_clip": 3.0,
    "working_document": 3.0,
    "agent_session_log": -5.0,
}

RECENCY_MAX_BOOST = 2.0
RECENCY_HALF_LIFE_DAYS = 30.0
LINEAGE_HALF_LIFE_DAYS = 90.0
LINEAGE_MAX_ABS_BOOST = 2.0
CONTEXT_PACKET_MAX_BYTES = {
    "compact": 16_000,
    "default": 32_000,
    "broad": 48_000,
    "inspect": 96_000,
}
NON_DEBUG_CITATION_LIMIT = 8
NON_DEBUG_CITATION_TEXT_CHARS = 320
DEBUG_CITATION_LIMIT = 24
DEBUG_CITATION_TEXT_CHARS = 800
RETRIEVAL_EVENT_QUERY_MAX_CHARS = 4_000
RETRIEVAL_EVENT_ID_LIMIT = 200
RETRIEVAL_EVENT_DEBUG_MAX_BYTES = 64_000
SEARCH_RESULT_SCORE_FLOOR = 12.0
CONTEXT_CHUNK_SCORE_FLOOR = 12.0
WIKI_PAGE_SCORE_FLOOR = 12.0
FOUND_CHUNK_SCORE = 24.0
FOUND_WIKI_SCORE = 24.0
FOUND_FACT_SCORE = 18.0
MEMORY_ACTIVE_SCORE_FLOOR = 1
MEMORY_CANDIDATE_SCORE_FLOOR = 2
FACT_SCORE_FLOOR = 12.0
FACT_TRUTH_CONFIDENCE_FLOOR = 0.5
FACT_FTS_SCORE_CAP = 12.0
FACT_KNEE_DROP = 10.0
FACT_KNEE_MIN_RANK = 3
NEGATIVE_CONTROL_CONTEXT_MARKERS = (
    "negative control",
    "negative-control",
    "fake topic",
    "fake topics",
    "absent topic",
    "absent topics",
    "likely absent",
    "should not be in brain",
    "no_strong_match",
)
NEGATIVE_CONTROL_QUERY_TERMS = {
    "absent",
    "control",
    "controls",
    "eval",
    "evaluation",
    "fake",
    "fixture",
    "fixtures",
    "negative",
    "retrieval",
    "topic",
    "topics",
}
MAX_CHUNKS_PER_DOCUMENT = 2
MANAGED_WIKI_BOOST = 8.0
SEMANTIC_WIKI_BOOST = 3.0
LINEAGE_EVENT_WEIGHTS = {
    "exposed": 0.0,
    "explicit_useful": 1.5,
    "explicit_not_useful": -1.75,
    "agent_referenced_id": 0.25,
    "memory_proposed_from_lineage": 0.0,
}
DEFAULT_RETRIEVAL_MODE = "default"
VALID_RETRIEVAL_MODES = {"compact", "default", "broad", "inspect"}
RECENCY_INTENT_TERMS = {
    "current",
    "last",
    "latest",
    "month",
    "new",
    "newest",
    "recent",
    "recently",
    "this",
    "today",
    "week",
    "yesterday",
}


@dataclass(frozen=True)
class RetrievalPolicy:
    mode: str
    total_budget: int
    max_chunks: int
    max_wiki_pages: int
    max_memories: int
    default_chunk_cap: int
    source_caps: dict[str, int]
    include_full_text: bool = False


class ReadOnlyModeError(RuntimeError):
    pass


class BrainService:
    def __init__(self, paths: BrainPaths, *, read_only: bool = False) -> None:
        self.paths = paths
        self.read_only = read_only
        self.embedding_config = load_embedding_config(paths)
        self.embedding_provider = resolve_embedding_provider(self.embedding_config)

    def _ensure_workspace(self) -> None:
        if self.read_only:
            if not self.paths.sqlite_path.exists():
                raise ReadOnlyModeError(
                    f"brain database is not available for read-only access: {self.paths.sqlite_path}"
                )
            return
        self.init_workspace()

    def _ensure_writable(self) -> None:
        if self.read_only:
            raise ReadOnlyModeError(
                "PKM Brain app is not available; write declined. Launch the app and retry."
            )

    def init_workspace(self) -> None:
        for directory in self.paths.directories():
            directory.mkdir(parents=True, exist_ok=True)
        init_db(self.paths.sqlite_path)
        if not self.paths.config_file.exists():
            self.paths.config_file.write_text(
                "embedding:\n"
                "  provider: hash\n"
                "  model: BAAI/bge-small-en-v1.5\n"
                '  query_instruction: ""\n',
                encoding="utf-8",
            )
        if not self.paths.golden_queries_file.exists():
            self.paths.golden_queries_file.write_text("[]\n", encoding="utf-8")

    def doctor(self) -> dict[str, Any]:
        embedding_status = self.embedding_provider.status(check_available=False)
        return {
            "home": str(self.paths.home),
            "directories": {
                path.name: path.exists() for path in self.paths.directories()
            },
            "sqlite": self.paths.sqlite_path.exists(),
            "lancedb": self.paths.lancedb_path.exists(),
            "embedding_provider": self.embedding_provider.name,
            "embedding": embedding_status,
            "nightly": self.nightly_doctor(),
        }

    def nightly_doctor(
        self, due_after_hours: int = 20, slack_hours: int = 4
    ) -> dict[str, Any]:
        if not self.paths.sqlite_path.exists():
            return {
                "job_name": "nightly-maintenance",
                "status": "unknown",
                "due_after_hours": due_after_hours,
                "slack_hours": slack_hours,
                "last_success": None,
                "last_failure": None,
                "last_success_age_hours": None,
                "warning": "SQLite database is missing",
            }
        with connection(self.paths.sqlite_path) as conn:
            last_success = conn.execute(
                """
                SELECT id, started_at, finished_at, status, error
                FROM automation_runs
                WHERE job_name = 'nightly-maintenance'
                  AND status = 'success'
                  AND finished_at IS NOT NULL
                ORDER BY finished_at DESC
                LIMIT 1
                """
            ).fetchone()
            last_failure = conn.execute(
                """
                SELECT id, started_at, finished_at, status, error
                FROM automation_runs
                WHERE job_name = 'nightly-maintenance'
                  AND status = 'failed'
                ORDER BY COALESCE(finished_at, started_at) DESC
                LIMIT 1
                """
            ).fetchone()
        success = automation_run_summary(last_success)
        failure = automation_run_summary(last_failure)
        age_hours = None
        warning = None
        status = "ok"
        if success and success.get("finished_at"):
            finished_at = parse_doctor_timestamp(str(success["finished_at"]))
            if finished_at:
                now = datetime.now(finished_at.tzinfo or timezone.utc)
                age_hours = round((now - finished_at).total_seconds() / 3600, 2)
                if age_hours > due_after_hours + slack_hours:
                    status = "warning"
                    warning = (
                        f"last successful nightly run was {age_hours:.1f}h ago; "
                        f"threshold is {due_after_hours + slack_hours}h"
                    )
        else:
            status = "warning"
            warning = "no successful nightly run recorded"
        return {
            "job_name": "nightly-maintenance",
            "status": status,
            "due_after_hours": due_after_hours,
            "slack_hours": slack_hours,
            "last_success": success,
            "last_failure": failure,
            "last_success_age_hours": age_hours,
            "warning": warning,
        }

    def download_embedding_model(self) -> dict[str, Any]:
        provider = resolve_embedding_provider(self.embedding_config, cache_only=False)
        if provider.provider != "sentence-transformer":
            return {
                "status": "skipped",
                "reason": "configured embedding provider is not sentence-transformer",
                "embedding": provider.status(check_available=False),
            }
        try:
            provider.embed(["health check"])
        except EmbeddingProviderUnavailable as exc:
            embedding = provider.status(check_available=False)
            embedding["available"] = False
            embedding["reason"] = str(exc)
            return {
                "status": "failed",
                "reason": str(exc),
                "embedding": embedding,
            }
        return {"status": "ok", "embedding": provider.status(check_available=False)}

    def index_doctor(self) -> dict[str, Any]:
        self.init_workspace()
        with connection(self.paths.sqlite_path) as conn:
            sqlite_chunk_ids = {
                row["id"]
                for row in conn.execute(
                    """
                    SELECT c.id
                    FROM chunks c
                    JOIN documents d ON d.id = c.document_id
                    WHERE d.status = 'active'
                    """
                )
            }
        stats = lancedb_stats(self.paths.lancedb_path)
        stamp_report = embedding_stamp_report(
            self.paths.lancedb_path,
            self.embedding_provider,
            table_exists=bool(stats["table_exists"]),
        )
        embedding_status = self.embedding_provider.status(check_available=False)
        try:
            lancedb_chunk_ids = vector_chunk_ids(self.paths.lancedb_path)
            vector_error = None
        except Exception as exc:
            lancedb_chunk_ids = set()
            vector_error = str(exc)

        missing_vectors = sorted(sqlite_chunk_ids - lancedb_chunk_ids)
        stale_vectors = sorted(lancedb_chunk_ids - sqlite_chunk_ids)
        reasons: list[str] = []
        status = "ok"
        if vector_error:
            status = "rebuild_recommended"
            reasons.append(f"could not enumerate LanceDB vectors: {vector_error}")
        if not embedding_status["available"]:
            status = "rebuild_recommended"
            reasons.append(
                f"embedding provider unavailable: {embedding_status['reason']}"
            )
        if stamp_report.get("reason"):
            status = "rebuild_recommended"
            reasons.append(str(stamp_report["reason"]))
        if sqlite_chunk_ids and not stats["table_exists"]:
            status = "rebuild_recommended"
            reasons.append("SQLite has chunks but LanceDB table is missing")
        if missing_vectors or stale_vectors:
            status = "rebuild_recommended"
            reasons.append("LanceDB chunk ids differ from SQLite chunks")
        elif should_optimize_vectors(stats):
            status = "optimize_recommended"
            reasons.append(
                "LanceDB table has accumulated versions, data files, or bytes above maintenance thresholds"
            )

        return {
            "status": status,
            "reasons": reasons,
            "sqlite_chunks": len(sqlite_chunk_ids),
            "lancedb": stats,
            "embedding": embedding_status,
            "embedding_stamp": stamp_report,
            "missing_vector_count": len(missing_vectors),
            "stale_vector_count": len(stale_vectors),
            "missing_vector_sample": missing_vectors[:20],
            "stale_vector_sample": stale_vectors[:20],
        }

    def optimize_indexes(self, cleanup_older_than_days: int = 1) -> dict[str, Any]:
        self.init_workspace()
        return optimize_vectors(
            self.paths.lancedb_path, cleanup_older_than_days=cleanup_older_than_days
        )

    def optimize_fts_indexes(self) -> dict[str, Any]:
        self.init_workspace()
        before = sqlite_storage_report(self.paths.sqlite_path)
        optimized: list[str] = []
        with connection(self.paths.sqlite_path) as conn:
            for table in ("chunk_fts", "retrieval_fts"):
                if not sqlite_table_exists(conn, table):
                    continue
                conn.execute(f"INSERT INTO {table}({table}) VALUES ('optimize')")
                optimized.append(table)
        after = sqlite_storage_report(self.paths.sqlite_path)
        return {
            "status": "ok" if optimized else "skipped",
            "optimized_tables": optimized,
            "before": before,
            "after": after,
            "bytes_freed": max(
                0,
                int(before["sqlite_related_bytes"])
                - int(after["sqlite_related_bytes"]),
            ),
            "errors": [],
        }

    def compact_retrieval_events(
        self,
        older_than_days: int = 90,
        *,
        automation_summary_older_than_days: int = 180,
        dry_run: bool = True,
        vacuum: bool = False,
    ) -> dict[str, Any]:
        if older_than_days < 0:
            raise ValueError("--older-than-days must be >= 0")
        if automation_summary_older_than_days < 0:
            raise ValueError("--automation-summary-older-than-days must be >= 0")
        self.init_workspace()
        retrieval_cutoff = retention_cutoff_iso(older_than_days)
        automation_cutoff = retention_cutoff_iso(automation_summary_older_than_days)
        before = sqlite_storage_report(self.paths.sqlite_path)
        with connection(self.paths.sqlite_path) as conn:
            retrieval_rows = rows(
                conn,
                """
                SELECT id, citation_snapshots, debug
                FROM retrieval_events
                WHERE timestamp < ?
                  AND (citation_snapshots != '[]' OR debug != '{}')
                ORDER BY timestamp
                """,
                (retrieval_cutoff,),
            )
            automation_rows = rows(
                conn,
                """
                SELECT id, summary
                FROM automation_runs
                WHERE COALESCE(finished_at, started_at) < ?
                  AND summary != '{}'
                ORDER BY COALESCE(finished_at, started_at)
                """,
                (automation_cutoff,),
            )
            retrieval_reclaimable = sum(
                max(0, len(str(row["citation_snapshots"] or "")) - len("[]"))
                + max(0, len(str(row["debug"] or "")) - len("{}"))
                for row in retrieval_rows
            )
            automation_reclaimable = sum(
                max(0, len(str(row["summary"] or "")) - len("{}"))
                for row in automation_rows
            )
            if not dry_run:
                conn.execute(
                    """
                    UPDATE retrieval_events
                    SET citation_snapshots = '[]',
                        debug = '{}'
                    WHERE timestamp < ?
                      AND (citation_snapshots != '[]' OR debug != '{}')
                    """,
                    (retrieval_cutoff,),
                )
                conn.execute(
                    """
                    UPDATE automation_runs
                    SET summary = '{}'
                    WHERE COALESCE(finished_at, started_at) < ?
                      AND summary != '{}'
                    """,
                    (automation_cutoff,),
                )
        vacuum_result: dict[str, Any] | None = None
        if vacuum and not dry_run:
            vacuum_before = sqlite_storage_report(self.paths.sqlite_path)
            with connection(self.paths.sqlite_path) as conn:
                conn.execute("VACUUM")
            vacuum_after = sqlite_storage_report(self.paths.sqlite_path)
            vacuum_result = {
                "before": vacuum_before,
                "after": vacuum_after,
                "bytes_freed": max(
                    0,
                    int(vacuum_before["sqlite_related_bytes"])
                    - int(vacuum_after["sqlite_related_bytes"]),
                ),
            }
        after = sqlite_storage_report(self.paths.sqlite_path)
        return {
            "status": "dry_run" if dry_run else "ok",
            "dry_run": dry_run,
            "retrieval_cutoff": retrieval_cutoff,
            "automation_summary_cutoff": automation_cutoff,
            "retrieval_events": {
                "eligible_rows": len(retrieval_rows),
                "payload_bytes_reclaimable": retrieval_reclaimable,
                "sample_ids": [str(row["id"]) for row in retrieval_rows[:10]],
            },
            "automation_runs": {
                "eligible_rows": len(automation_rows),
                "summary_bytes_reclaimable": automation_reclaimable,
                "sample_ids": [str(row["id"]) for row in automation_rows[:10]],
            },
            "total_payload_bytes_reclaimable": retrieval_reclaimable
            + automation_reclaimable,
            "before": before,
            "after": after,
            "vacuum": vacuum_result,
            "stale_db_backups": self.stale_db_backup_report(),
            "errors": [],
        }

    def stale_db_backup_report(self) -> dict[str, Any]:
        backups = (
            sorted(self.paths.db_dir.glob("*.bak.gz"))
            if self.paths.db_dir.exists()
            else []
        )
        files = [
            {
                "path": str(path),
                "bytes": path_size(path),
                "modified_at": datetime.fromtimestamp(
                    path.stat().st_mtime, timezone.utc
                ).isoformat(),
            }
            for path in backups
        ]
        return {
            "status": "human_review_required" if files else "ok",
            "count": len(files),
            "total_bytes": sum(int(item["bytes"]) for item in files),
            "files": files[:20],
            "note": "Flag only; pkm-brain does not delete db/*.bak.gz backups automatically.",
        }

    def _vector_rows_for_chunks(
        self, chunk_rows: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        if not chunk_rows:
            return []
        texts = [
            str(row["text"])
            if self.embedding_provider.provider == HASH_PROVIDER
            else passage_embedding_text(
                str(row["text"]),
                str(row["heading_path"] if "heading_path" in row.keys() else ""),
            )
            for row in chunk_rows
        ]
        vectors = self.embedding_provider.embed(texts)
        return [
            {
                "chunk_id": chunk_row_id(row),
                "document_id": row["document_id"],
                "text": row["text"],
                "vector": vector,
            }
            for row, vector in zip(chunk_rows, vectors)
        ]

    def rebuild_vector_index(
        self,
        delete_backup: bool = False,
        batch_size: int = 128,
        missing_only: bool = False,
    ) -> dict[str, Any]:
        self.init_workspace()
        before = lancedb_stats(self.paths.lancedb_path)
        embedding_status = self.embedding_provider.status(check_available=True)
        with connection(self.paths.sqlite_path) as conn:
            chunk_rows = rows(
                conn,
                """
                SELECT c.id, c.document_id, c.text, c.heading_path
                FROM chunks c
                JOIN documents d ON d.id = c.document_id
                WHERE d.status = 'active'
                ORDER BY c.document_id, c.chunk_index
                """,
            )

        if not embedding_status["available"]:
            return {
                "status": "skipped",
                "reason": f"embedding provider unavailable: {embedding_status['reason']}",
                "sqlite_chunks": len(chunk_rows),
                "vectors_written": 0,
                "before": before,
                "after": lancedb_stats(self.paths.lancedb_path),
                "embedding": embedding_status,
            }

        stale_vectors_removed = 0
        if missing_only:
            try:
                existing_ids = vector_chunk_ids(self.paths.lancedb_path)
                active_ids = {str(row["id"]) for row in chunk_rows}
                stale_ids = sorted(existing_ids - active_ids)
                for offset in range(0, len(stale_ids), 500):
                    stale_vectors_removed += delete_vectors(
                        self.paths.lancedb_path, stale_ids[offset : offset + 500]
                    )
                existing_ids.difference_update(stale_ids)
                target_rows = [
                    row for row in chunk_rows if row["id"] not in existing_ids
                ]
            except Exception as exc:
                return {
                    "status": "failed",
                    "error": str(exc),
                    "sqlite_chunks": len(chunk_rows),
                    "vectors_written": 0,
                    "before": before,
                    "after": lancedb_stats(self.paths.lancedb_path),
                    "embedding": embedding_status,
                }
        else:
            target_rows = chunk_rows

        backup_path = None
        failed_path = None
        if not missing_only and self.paths.lancedb_path.exists():
            backup_path = unique_backup_path(self.paths.indexes, "lancedb.backup")
            shutil.move(str(self.paths.lancedb_path), str(backup_path))

        try:
            embedded = 0
            for offset in range(0, len(target_rows), batch_size):
                batch = target_rows[offset : offset + batch_size]
                embedded += upsert_vectors(
                    self.paths.lancedb_path,
                    self._vector_rows_for_chunks(batch),
                    self.embedding_provider,
                )
            after = lancedb_stats(self.paths.lancedb_path)
            if int(after["rows"]) != len(chunk_rows):
                raise RuntimeError(
                    f"rebuilt LanceDB row count {after['rows']} did not match SQLite chunks {len(chunk_rows)}"
                )
            if delete_backup and backup_path and backup_path.exists():
                shutil.rmtree(backup_path)
                backup_retained = False
            else:
                backup_retained = bool(backup_path and backup_path.exists())
            return {
                "status": "ok",
                "sqlite_chunks": len(chunk_rows),
                "vectors_written": embedded,
                "before": before,
                "after": after,
                "embedding": embedding_status,
                "missing_only": missing_only,
                "stale_vectors_removed": stale_vectors_removed,
                "backup_path": str(backup_path) if backup_path else None,
                "backup_retained": backup_retained,
            }
        except Exception as exc:
            if not missing_only and self.paths.lancedb_path.exists():
                failed_path = unique_backup_path(self.paths.indexes, "lancedb.failed")
                shutil.move(str(self.paths.lancedb_path), str(failed_path))
            if not missing_only and backup_path and backup_path.exists():
                shutil.move(str(backup_path), str(self.paths.lancedb_path))
            return {
                "status": "failed",
                "error": str(exc),
                "sqlite_chunks": len(chunk_rows),
                "before": before,
                "after": lancedb_stats(self.paths.lancedb_path),
                "embedding": embedding_status,
                "missing_only": missing_only,
                "stale_vectors_removed": stale_vectors_removed,
                "backup_path": str(backup_path) if backup_path else None,
                "failed_path": str(failed_path) if failed_path else None,
            }

    def reset_retrieval_index(self) -> dict[str, Any]:
        self.init_workspace()
        with connection(self.paths.sqlite_path) as conn:
            document_rows = rows(
                conn,
                """
                SELECT *
                FROM documents
                WHERE status = 'active'
                ORDER BY ingested_at, id
                """,
            )

        plans: list[dict[str, Any]] = []
        errors: list[str] = []
        for row in document_rows:
            document = dict(row)
            source_path = document_text_path(document)
            if source_path is None:
                errors.append(f"{document['id']}: no readable raw_path or source_path")
                continue
            try:
                text = source_path.read_text(encoding="utf-8", errors="replace")
                plans.append(
                    {
                        "document": document,
                        "text_path": source_path,
                        "content_hash": file_sha256(source_path),
                        "chunks": chunk_text(text, str(document["source_type"])),
                    }
                )
            except Exception as exc:
                errors.append(f"{document['id']}: {source_path}: {exc}")

        if errors:
            return {
                "status": "failed",
                "documents": len(document_rows),
                "documents_planned": len(plans),
                "chunks_planned": sum(len(plan["chunks"]) for plan in plans),
                "errors": errors,
            }

        reset_at = now_iso()
        chunks_created = 0
        with connection(self.paths.sqlite_path) as conn:
            retrieval_events_deleted = conn.execute(
                "DELETE FROM retrieval_events"
            ).rowcount
            lineage_events_deleted = conn.execute(
                "DELETE FROM context_lineage_events WHERE retrieval_event_id IS NOT NULL"
            ).rowcount
            delete_all_chunk_fts(conn)
            active_document_ids = [
                str(document["id"]) for document in document_rows
            ]
            if active_document_ids:
                placeholders = ",".join("?" for _ in active_document_ids)
                chunks_deleted = conn.execute(
                    f"DELETE FROM chunks WHERE document_id IN ({placeholders})",
                    active_document_ids,
                ).rowcount
            else:
                chunks_deleted = 0

            for plan in plans:
                document = plan["document"]
                conn.execute(
                    """
                    UPDATE documents
                    SET content_hash = ?
                    WHERE id = ?
                    """,
                    (plan["content_hash"], document["id"]),
                )
                for chunk in plan["chunks"]:
                    chunk_id = new_id("chunk")
                    conn.execute(
                        """
                        INSERT INTO chunks(
                          id, document_id, chunk_index, corpus_type, text, heading_path,
                          start_offset, end_offset, token_count, content_hash, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            chunk_id,
                            document["id"],
                            chunk.chunk_index,
                            "raw",
                            chunk.text,
                            chunk.heading_path,
                            chunk.start_offset,
                            chunk.end_offset,
                            chunk.token_count,
                            chunk.content_hash,
                            reset_at,
                        ),
                    )
                    insert_chunk_retrieval_fts(
                        conn,
                        chunk_id=chunk_id,
                        title=document["title"],
                        text=chunk.text,
                        heading_path=chunk.heading_path,
                        project=document.get("project") or "",
                        tags=document.get("tags") or "",
                    )
                    chunks_created += 1

        if self.paths.lancedb_path.exists():
            if self.paths.lancedb_path.is_dir():
                shutil.rmtree(self.paths.lancedb_path)
            else:
                self.paths.lancedb_path.unlink()
        vector_rebuild = self.rebuild_vector_index(delete_backup=True)
        doctor = self.index_doctor()
        vector_rebuild_skipped = vector_rebuild[
            "status"
        ] == "skipped" and "embedding provider unavailable" in str(
            vector_rebuild.get("reason") or ""
        )
        status = (
            "ok"
            if (
                vector_rebuild["status"] == "ok"
                and doctor["status"] in {"ok", "optimize_recommended"}
            )
            or vector_rebuild_skipped
            else "failed"
        )
        return {
            "status": status,
            "documents": len(document_rows),
            "chunks_deleted": chunks_deleted,
            "chunks_created": chunks_created,
            "retrieval_events_deleted": retrieval_events_deleted,
            "retrieval_lineage_events_deleted": lineage_events_deleted,
            "vector_rebuild": vector_rebuild,
            "index_doctor": doctor,
            "errors": []
            if status == "ok"
            else vector_rebuild.get("errors", [])
            or [vector_rebuild.get("error", "index reset failed")],
        }

    def reindex_chunks(
        self,
        source_type: str = "agent_session_log",
        dry_run: bool = False,
        all_documents: bool = False,
        target_tokens: int = DEFAULT_TARGET_TOKENS,
        overlap_tokens: int = DEFAULT_OVERLAP_TOKENS,
    ) -> dict[str, Any]:
        self.init_workspace()
        if target_tokens <= 0:
            raise ValueError("target_tokens must be positive")
        if overlap_tokens < 0 or overlap_tokens >= target_tokens:
            raise ValueError("overlap_tokens must be >= 0 and less than target_tokens")

        with connection(self.paths.sqlite_path) as conn:
            query = """
            SELECT d.id, d.source_type, d.title, d.source_path, d.raw_path,
                   COUNT(c.id) AS current_chunks,
                   MAX(c.token_count) AS max_chunk_tokens,
                   MAX(LENGTH(c.text)) AS max_chunk_bytes
            FROM documents d
            JOIN chunks c ON c.document_id = d.id
            WHERE d.source_type = ? AND d.status = 'active'
            GROUP BY d.id
            """
            params: tuple[Any, ...] = (source_type,)
            if not all_documents:
                query += " HAVING MAX(c.token_count) > ?"
                params = (source_type, target_tokens)
            query += " ORDER BY max_chunk_tokens DESC, d.ingested_at DESC"
            affected = rows(conn, query, params)
            plans: list[dict[str, Any]] = []
            errors: list[str] = []
            total_projected_chunks = 0
            total_current_chunks = 0
            max_projected_tokens = 0

            for row in affected:
                document = dict(row)
                source_path = document_text_path(document)
                if source_path is None:
                    errors.append(
                        f"{document['id']}: no readable raw_path or source_path"
                    )
                    continue
                text = source_path.read_text(encoding="utf-8", errors="replace")
                projected_chunks = chunk_text(
                    text,
                    str(document["source_type"]),
                    target_tokens=target_tokens,
                    overlap_tokens=overlap_tokens,
                )
                projected_max_tokens = max(
                    (chunk.token_count for chunk in projected_chunks), default=0
                )
                plan = {
                    "document_id": document["id"],
                    "title": document["title"],
                    "source_type": document["source_type"],
                    "source_path": document["source_path"],
                    "raw_path": document["raw_path"],
                    "text_path": str(source_path),
                    "current_chunks": int(document["current_chunks"]),
                    "projected_chunks": len(projected_chunks),
                    "max_chunk_tokens": int(document["max_chunk_tokens"] or 0),
                    "max_chunk_bytes": int(document["max_chunk_bytes"] or 0),
                    "projected_max_chunk_tokens": projected_max_tokens,
                    "_chunks": projected_chunks,
                }
                plans.append(plan)
                total_current_chunks += plan["current_chunks"]
                total_projected_chunks += len(projected_chunks)
                max_projected_tokens = max(max_projected_tokens, projected_max_tokens)

            public_documents = [
                {key: value for key, value in plan.items() if key != "_chunks"}
                for plan in plans
            ]
            summary = {
                "status": "dry_run" if dry_run else "ok",
                "dry_run": dry_run,
                "source_type": source_type,
                "all_documents": all_documents,
                "target_tokens": target_tokens,
                "overlap_tokens": overlap_tokens,
                "affected_documents": len(plans),
                "current_chunks": total_current_chunks,
                "projected_chunks": total_projected_chunks,
                "max_projected_chunk_tokens": max_projected_tokens,
                "errors": errors,
                "documents": public_documents,
            }
            if dry_run or not plans or errors:
                if errors and not dry_run:
                    summary["status"] = "failed"
                return summary

            rewritten_chunks = 0
            reindexed_at = now_iso()
            for plan in plans:
                document_id = str(plan["document_id"])
                old_chunk_ids = [
                    row["id"]
                    for row in conn.execute(
                        "SELECT id FROM chunks WHERE document_id = ? ORDER BY chunk_index",
                        (document_id,),
                    )
                ]
                if old_chunk_ids:
                    delete_chunk_retrieval_fts(conn, old_chunk_ids)
                conn.execute("DELETE FROM chunks WHERE document_id = ?", (document_id,))

                for chunk in plan["_chunks"]:
                    chunk_id = new_id("chunk")
                    conn.execute(
                        """
                        INSERT INTO chunks(
                          id, document_id, chunk_index, corpus_type, text, heading_path,
                          start_offset, end_offset, token_count, content_hash, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            chunk_id,
                            document_id,
                            chunk.chunk_index,
                            "raw",
                            chunk.text,
                            chunk.heading_path,
                            chunk.start_offset,
                            chunk.end_offset,
                            chunk.token_count,
                            chunk.content_hash,
                            reindexed_at,
                        ),
                    )
                    insert_chunk_retrieval_fts(
                        conn,
                        chunk_id=chunk_id,
                        title=plan["title"],
                        text=chunk.text,
                        heading_path=chunk.heading_path,
                        project="",
                        tags="",
                    )
                    rewritten_chunks += 1

        if dry_run or not plans or errors:
            return summary

        rebuild = self.rebuild_vector_index()
        doctor = self.index_doctor()
        summary.update(
            {
                "status": "ok"
                if rebuild["status"] == "ok"
                and doctor["status"] in {"ok", "optimize_recommended"}
                else "rebuild_recommended",
                "rewritten_chunks": rewritten_chunks,
                "index_rebuild": rebuild,
                "index_doctor": doctor,
            }
        )
        return summary

    def sync_doctor(self) -> dict[str, Any]:
        from .sync_doctor import run_sync_doctor

        return run_sync_doctor(self.paths).as_dict()

    def record_sync_run(
        self,
        peer_node_id: str,
        direction: str,
        started_at: str,
        finished_at: str,
        status: str,
        files_pulled: int = 0,
        files_pushed: int = 0,
        bytes_pulled: int = 0,
        bytes_pushed: int = 0,
        primary_ingest_run_id: str | None = None,
        remote_ingest_status: str | None = None,
        errors: list[str] | None = None,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        self.init_workspace()
        sync_run_id = run_id or new_id("sync_run")
        payload = {
            "id": sync_run_id,
            "peer_node_id": peer_node_id,
            "direction": direction,
            "started_at": started_at,
            "finished_at": finished_at,
            "status": status,
            "files_pulled": int(files_pulled),
            "files_pushed": int(files_pushed),
            "bytes_pulled": int(bytes_pulled),
            "bytes_pushed": int(bytes_pushed),
            "primary_ingest_run_id": primary_ingest_run_id,
            "remote_ingest_status": remote_ingest_status,
            "errors": errors or [],
        }
        with connection(self.paths.sqlite_path) as conn:
            conn.execute(
                """
                INSERT INTO sync_runs(
                  id, peer_node_id, direction, started_at, finished_at, status,
                  files_pulled, files_pushed, bytes_pulled, bytes_pushed,
                  primary_ingest_run_id, remote_ingest_status, errors
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    payload["id"],
                    payload["peer_node_id"],
                    payload["direction"],
                    payload["started_at"],
                    payload["finished_at"],
                    payload["status"],
                    payload["files_pulled"],
                    payload["files_pushed"],
                    payload["bytes_pulled"],
                    payload["bytes_pushed"],
                    payload["primary_ingest_run_id"],
                    payload["remote_ingest_status"],
                    dumps(payload["errors"]),
                ),
            )
        return payload

    def sync_status(self) -> dict[str, Any]:
        from .sync_status import sync_status

        return sync_status(self.paths)

    def sync_conflicts(self) -> dict[str, Any]:
        from .sync_status import sync_conflicts

        return sync_conflicts(self.paths)

    def ingest(
        self,
        source: Path | None = None,
        dry_run: bool = False,
        origin_node_id: str | None = None,
        retry_quarantine: bool = False,
    ) -> IngestResult:
        self.init_workspace()
        if retry_quarantine and not dry_run:
            self.retry_quarantine()
        source = source.expanduser().resolve() if source else self.paths.inbox
        if source.is_file():
            candidates = [source]
        else:
            candidates = sorted(
                path
                for path in source.rglob("*")
                if path.is_file()
                and not path.name.startswith(".")
                and not reserved_ingest_path(path)
            )
        run_id = new_id("run")
        started = now_iso()
        errors: list[str] = []
        changed = 0
        skipped = 0
        chunks_created = 0
        documents_replaced = 0
        vector_source_rows: list[dict[str, Any]] = []
        stale_vector_chunk_ids: list[str] = []

        if dry_run:
            return IngestResult(run_id, len(candidates), 0, 0, 0, 0, [])

        with connection(self.paths.sqlite_path) as conn:
            conn.execute(
                "INSERT INTO ingestion_runs(id, started_at, status, documents_discovered) VALUES (?, ?, ?, ?)",
                (run_id, started, "running", len(candidates)),
            )
            for path in candidates:
                try:
                    source_type = detect_source_type(path)
                    if not source_type:
                        skipped += 1
                        continue
                    stat = path.stat()
                    source_mtime_ns = int(stat.st_mtime_ns)
                    source_size = int(stat.st_size)
                    origin, logical_source_key = self._origin_identity_for_path(
                        path, origin_node_id, source_type
                    )
                    existing = conn.execute(
                        """
                        SELECT id, source_type, title, content_hash, raw_path, source_mtime_ns, source_size
                        FROM documents
                        WHERE logical_source_key = ? AND (origin_node_id = ? OR (? = 'gmail_thread' AND ? = 'gmail-knowledge'))
                        ORDER BY CASE WHEN origin_node_id = ? THEN 0 ELSE 1 END, ingested_at DESC
                        LIMIT 1
                        """,
                        (logical_source_key, origin, source_type, origin, origin),
                    ).fetchone()
                    if existing and existing_document_matches_source_stats(
                        existing,
                        source_mtime_ns=source_mtime_ns,
                        source_size=source_size,
                    ):
                        if source_type == "agent_session_log":
                            replaced = remove_superseded_agent_session_snapshots(
                                conn,
                                path,
                                origin_node_id=origin,
                                keep_document_id=existing["id"],
                            )
                            stale_vector_chunk_ids.extend(replaced.chunk_ids)
                            documents_replaced += replaced.documents
                        refresh_existing_document_metadata(
                            conn,
                            existing["id"],
                            source_type,
                            str(existing["title"]),
                            path,
                            origin,
                            logical_source_key,
                            source_mtime_ns=source_mtime_ns,
                            source_size=source_size,
                        )
                        skipped += 1
                        continue
                    content_hash = file_sha256(path)
                    text = path.read_text(encoding="utf-8", errors="replace")
                    title = document_title_for_text(text, path)
                    document_tags = gmail_document_tags(text, source_type)
                    if existing:
                        if existing["content_hash"] == content_hash:
                            if source_type == "agent_session_log":
                                replaced = remove_superseded_agent_session_snapshots(
                                    conn,
                                    path,
                                    origin_node_id=origin,
                                    keep_document_id=existing["id"],
                                )
                                stale_vector_chunk_ids.extend(replaced.chunk_ids)
                                documents_replaced += replaced.documents
                            refresh_existing_document_metadata(
                                conn,
                                existing["id"],
                                source_type,
                                title,
                                path,
                                origin,
                                logical_source_key,
                                source_mtime_ns=source_mtime_ns,
                                source_size=source_size,
                            )
                            skipped += 1
                            continue
                        replaced = remove_documents(conn, [dict(existing)])
                        if source_type == "agent_session_log":
                            superseded = remove_superseded_agent_session_snapshots(
                                conn, path, origin_node_id=origin
                            )
                            replaced = ReplacedDocuments(
                                replaced.documents + superseded.documents,
                                replaced.chunk_ids + superseded.chunk_ids,
                            )
                        if replaced.documents:
                            stale_vector_chunk_ids.extend(replaced.chunk_ids)
                            documents_replaced += replaced.documents
                    document_id = new_id("doc")
                    ingested_at = now_iso()
                    if source_type == "agent_session_log":
                        replaced = remove_superseded_agent_session_snapshots(
                            conn, path, origin_node_id=origin
                        )
                        stale_vector_chunk_ids.extend(replaced.chunk_ids)
                        documents_replaced += replaced.documents
                    raw_path = self._copy_raw(
                        path, source_type, ingested_at, content_hash
                    )
                    conn.execute(
                        """
                        INSERT INTO documents(
                          id, source_type, title, source_path, raw_path, content_hash,
                          origin_node_id, logical_source_key, source_mtime_ns, source_size,
                          created_at, ingested_at, project, tags, version, status
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            document_id,
                            source_type,
                            title,
                            str(path),
                            str(raw_path),
                            content_hash,
                            origin,
                            logical_source_key,
                            source_mtime_ns,
                            source_size,
                            ingested_at,
                            ingested_at,
                            None,
                            dumps(document_tags),
                            1,
                            "active",
                        ),
                    )
                    doc_chunks = chunk_text(text, source_type)
                    for chunk in doc_chunks:
                        chunk_id = new_id("chunk")
                        conn.execute(
                            """
                            INSERT INTO chunks(
                              id, document_id, chunk_index, corpus_type, text, heading_path,
                              start_offset, end_offset, token_count, content_hash, created_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                chunk_id,
                                document_id,
                                chunk.chunk_index,
                                "raw",
                                chunk.text,
                                chunk.heading_path,
                                chunk.start_offset,
                                chunk.end_offset,
                                chunk.token_count,
                                chunk.content_hash,
                                ingested_at,
                            ),
                        )
                        insert_chunk_retrieval_fts(
                            conn,
                            chunk_id=chunk_id,
                            title=title,
                            text=chunk.text,
                            heading_path=chunk.heading_path,
                            project="",
                            tags="",
                        )
                        vector_source_rows.append(
                            {
                                "chunk_id": chunk_id,
                                "document_id": document_id,
                                "text": chunk.text,
                                "heading_path": chunk.heading_path,
                            }
                        )
                    if source_type == "agent_session_log":
                        record_agent_log_lineage_references(
                            conn,
                            text=prepare_text_for_indexing(text, source_type),
                            document_id=document_id,
                            agent_session_id=markdown_frontmatter_value(
                                text, "session_id"
                            ),
                            created_at=ingested_at,
                        )
                    changed += 1
                    chunks_created += len(doc_chunks)
                except Exception as exc:
                    if self._quarantine_external_ingest_failure(path, exc):
                        errors.append(
                            f"{path}: quarantined after ingest failure: {exc}"
                        )
                    else:
                        errors.append(f"{path}: {exc}")
            delete_vectors(self.paths.lancedb_path, stale_vector_chunk_ids)
            embeddings_created = 0
            vector_writes = {
                "status": "ok",
                "reason": None,
                "attempted": len(vector_source_rows),
            }
            try:
                for offset in range(0, len(vector_source_rows), 128):
                    batch = vector_source_rows[offset : offset + 128]
                    embeddings_created += upsert_vectors(
                        self.paths.lancedb_path,
                        self._vector_rows_for_chunks(batch),
                        self.embedding_provider,
                    )
                vector_writes["written"] = embeddings_created
            except (EmbeddingProviderUnavailable, VectorIndexUnavailable) as exc:
                vector_writes = {
                    "status": "skipped",
                    "reason": str(exc),
                    "attempted": len(vector_source_rows),
                    "written": 0,
                }
            conn.execute(
                """
                UPDATE ingestion_runs
                SET finished_at = ?, status = ?, documents_changed = ?, documents_skipped = ?,
                    chunks_created = ?, embeddings_created = ?, errors = ?
                WHERE id = ?
                """,
                (
                    now_iso(),
                    "failed" if errors else "success",
                    changed,
                    skipped,
                    chunks_created,
                    embeddings_created,
                    dumps(errors),
                    run_id,
                ),
            )

        return IngestResult(
            run_id,
            len(candidates),
            changed,
            skipped,
            chunks_created,
            embeddings_created,
            errors,
            documents_replaced,
            vector_writes,
        )

    def rebuild_mirror_index(self) -> dict[str, Any]:
        self.init_workspace()
        raw_root = self.paths.raw.resolve()
        candidates = sorted(
            path
            for path in self.paths.raw.rglob("*")
            if path.is_file()
            and not path.name.startswith(".")
            and not reserved_ingest_path(path)
        )
        run_id = new_id("run")
        started = now_iso()
        errors: list[str] = []
        indexed = 0
        skipped = 0
        chunks_created = 0

        with connection(self.paths.sqlite_path) as conn:
            conn.execute(
                "INSERT INTO ingestion_runs(id, started_at, status, documents_discovered) VALUES (?, ?, ?, ?)",
                (run_id, started, "running", len(candidates)),
            )
            remove_mirror_index_documents(conn, raw_root)

            for path in candidates:
                try:
                    source_type = detect_source_type(path)
                    if not source_type:
                        skipped += 1
                        continue
                    content_hash = file_sha256(path)
                    text = path.read_text(encoding="utf-8", errors="replace")
                    title = document_title_for_text(text, path)
                    relative_path = path.resolve().relative_to(raw_root).as_posix()
                    document_id = deterministic_mirror_id("doc", relative_path)
                    ingested_at = now_iso()
                    conn.execute(
                        """
                        INSERT INTO documents(
                          id, source_type, title, source_path, raw_path, content_hash,
                          origin_node_id, logical_source_key, created_at, ingested_at,
                          project, tags, version, status
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            document_id,
                            source_type,
                            title,
                            str(path),
                            str(path),
                            content_hash,
                            "mirror",
                            relative_path,
                            ingested_at,
                            ingested_at,
                            None,
                            dumps([]),
                            1,
                            "active",
                        ),
                    )
                    doc_chunks = chunk_text(text, source_type)
                    for chunk in doc_chunks:
                        chunk_id = deterministic_mirror_id(
                            "chunk",
                            f"{relative_path}:{chunk.chunk_index}:{chunk.content_hash}",
                        )
                        conn.execute(
                            """
                            INSERT INTO chunks(
                              id, document_id, chunk_index, corpus_type, text, heading_path,
                              start_offset, end_offset, token_count, content_hash, created_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                chunk_id,
                                document_id,
                                chunk.chunk_index,
                                "raw",
                                chunk.text,
                                chunk.heading_path,
                                chunk.start_offset,
                                chunk.end_offset,
                                chunk.token_count,
                                chunk.content_hash,
                                ingested_at,
                            ),
                        )
                        insert_chunk_retrieval_fts(
                            conn,
                            chunk_id=chunk_id,
                            title=title,
                            text=chunk.text,
                            heading_path=chunk.heading_path,
                            project="",
                            tags="",
                        )
                    if source_type == "agent_session_log":
                        record_agent_log_lineage_references(
                            conn,
                            text=prepare_text_for_indexing(text, source_type),
                            document_id=document_id,
                            agent_session_id=markdown_frontmatter_value(
                                text, "session_id"
                            ),
                            created_at=ingested_at,
                        )
                    indexed += 1
                    chunks_created += len(doc_chunks)
                except Exception as exc:
                    errors.append(f"{path}: {exc}")

        vector_rebuild = self.rebuild_vector_index(delete_backup=True)
        embeddings_created = (
            int(vector_rebuild.get("vectors_written") or 0)
            if vector_rebuild["status"] == "ok"
            else 0
        )
        if vector_rebuild["status"] not in {"ok", "skipped"}:
            errors.append(str(vector_rebuild.get("error") or "vector rebuild failed"))

        with connection(self.paths.sqlite_path) as conn:
            conn.execute(
                """
                UPDATE ingestion_runs
                SET finished_at = ?, status = ?, documents_changed = ?, documents_skipped = ?,
                    chunks_created = ?, embeddings_created = ?, errors = ?
                WHERE id = ?
                """,
                (
                    now_iso(),
                    "failed" if errors else "success",
                    indexed,
                    skipped,
                    chunks_created,
                    embeddings_created,
                    dumps(errors),
                    run_id,
                ),
            )

        from .wiki import lint_wiki

        wiki = lint_wiki(self.paths)
        memories = self.import_memories(self.paths.memory, allow_missing_sources=True)
        return {
            "run_id": run_id,
            "raw_files_discovered": len(candidates),
            "documents_indexed": indexed,
            "documents_skipped": skipped,
            "chunks_created": chunks_created,
            "embeddings_created": embeddings_created,
            "errors": errors,
            "vector_rebuild": vector_rebuild,
            "wiki": wiki,
            "memories": memories,
        }

    def _copy_raw(
        self, source: Path, source_type: str, ingested_at: str, content_hash: str
    ) -> Path:
        date = ingested_at[:10].split("-")
        target_dir = self.paths.raw / source_type / date[0] / date[1]
        target_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(target_dir, 0o700)
        secure_gmail_raw_directories(target_dir, self.paths.raw, source_type)
        target = (
            target_dir / f"{slugify(source.stem)}-{content_hash[:12]}{source.suffix}"
        )
        shutil.copy2(source, target)
        return target

    def _origin_identity_for_path(self, path: Path, origin_node_id: str | None = None, source_type: str | None = None) -> tuple[str, str]:
        try:
            external_relative = path.resolve().relative_to(
                (self.paths.inbox / "external").resolve()
            )
        except ValueError:
            external_relative = None
        origin = origin_node_id or ("gmail-knowledge" if source_type == "gmail_thread" else local_node_id(self.paths))
        if external_relative and external_relative.parts:
            origin = origin_node_id or external_relative.parts[0]
            if len(external_relative.parts) > 1:
                return origin, Path(*external_relative.parts[1:]).as_posix()
        return origin, str(path)

    def retry_quarantine(self) -> dict[str, Any]:
        external_root = self.paths.inbox / "external"
        restored: list[str] = []
        if not external_root.exists():
            return {"restored": restored}
        for quarantine_root in sorted(external_root.glob("*/_quarantine")):
            peer_root = quarantine_root.parent
            for path in sorted(quarantine_root.rglob("*")):
                if (
                    not path.is_file()
                    or path.name.endswith(".error.json")
                    or path.name.startswith(".")
                ):
                    continue
                relative_path = path.relative_to(quarantine_root)
                target = peer_root / relative_path
                target.parent.mkdir(parents=True, exist_ok=True)
                path.replace(target)
                error_path = quarantine_error_path(quarantine_root, relative_path)
                if error_path.exists():
                    error_path.unlink()
                restored.append(str(target))
            remove_empty_dirs(quarantine_root)
        return {"restored": restored}

    def _quarantine_external_ingest_failure(self, path: Path, exc: Exception) -> bool:
        info = external_inbox_path_info(self.paths, path)
        if info is None:
            return False
        peer_root, relative_path = info
        if relative_path.parts and relative_path.parts[0] in {
            "_staging",
            "_quarantine",
            "_rejected",
        }:
            return False
        if not path.exists():
            return False
        quarantine_root = peer_root / "_quarantine"
        target = quarantine_root / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        path.replace(target)
        error_path = quarantine_error_path(quarantine_root, relative_path)
        error_payload = {
            "source_path": str(path),
            "quarantined_path": str(target),
            "error": str(exc),
            "traceback": "".join(
                traceback.format_exception_only(type(exc), exc)
            ).strip(),
        }
        error_path.write_text(
            json.dumps(error_payload, sort_keys=True, indent=2) + "\n", encoding="utf-8"
        )
        return True

    def search(
        self,
        query: str,
        limit: int = 10,
        debug: bool = False,
        caller: str = "cli",
        *,
        record_telemetry: bool = True,
    ) -> dict[str, Any]:
        self._ensure_workspace()
        fanout_limit = max(60, limit * 6)
        chunk_candidates, fanout_debug = self._fanout_chunk_candidates(
            query, limit=fanout_limit
        )
        lineage_scores = self._lineage_scores_for_chunks(chunk_candidates)
        reranked_chunks = rerank_chunks(
            query, chunk_candidates, fanout_debug, lineage_scores=lineage_scores
        )
        selected = select_search_results(reranked_chunks, limit=limit)
        wiki_pages = self.select_wiki_pages(query, selected, limit=min(limit, 5))
        selected_ids = [row["chunk_id"] for row in selected]
        citation_snapshots = chunk_citation_snapshots(
            selected
        ) + wiki_page_citation_snapshots(wiki_pages)
        search_debug = build_search_debug(
            fanout_debug, selected, reranked_chunks, debug=debug
        )
        search_debug["selected_wiki_reasons"] = [
            {
                "relative_path": page.get("relative_path"),
                "score": page.get("score"),
                "reasons": page.get("selection_reasons", []),
            }
            for page in wiki_pages
        ]
        assessment = retrieval_assessment(selected, wiki_pages, [], [])
        search_debug["assessment"] = assessment
        if fanout_debug.get("vector_unavailable_reason"):
            assessment["reasons"].append(
                f"vector_search unavailable: {fanout_debug['vector_unavailable_reason']}"
            )
        event_id = None
        if not self.read_only and record_telemetry:
            event_id = new_id("retrieval")
            with connection(self.paths.sqlite_path) as conn:
                conn.execute(
                    """
                    INSERT INTO retrieval_events(
                      id, query, timestamp, caller, returned_chunk_ids, selected_chunk_ids, citation_snapshots, debug
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event_id,
                        truncate_for_packet(query, RETRIEVAL_EVENT_QUERY_MAX_CHARS),
                        now_iso(),
                        caller,
                        dumps(
                            list(fanout_debug["candidate_ids"])[
                                :RETRIEVAL_EVENT_ID_LIMIT
                            ]
                        ),
                        dumps(selected_ids[:RETRIEVAL_EVENT_ID_LIMIT]),
                        dumps(
                            stored_citation_snapshots(citation_snapshots, debug=debug)
                        ),
                        stored_retrieval_debug(search_debug) if debug else "{}",
                    ),
                )
        return {
            "event_id": event_id,
            "query": query,
            "retrieval_verdict": assessment["verdict"],
            "retrieval_confidence": assessment["confidence"],
            "retrieval_reasons": assessment["reasons"],
            "results": selected,
            "relevant_wiki_pages": wiki_pages,
            "citation_snapshots": citation_snapshots,
            "debug": search_debug if debug else None,
        }

    def retrieve_context(
        self,
        task: str,
        project: str | None = None,
        budget: int | None = None,
        mode: str = DEFAULT_RETRIEVAL_MODE,
        debug: bool = False,
        *,
        valid_as_of: str | None = None,
        known_as_of: str | None = None,
        event_as_of: str | None = None,
        event_kind: str | None = None,
        temporal_mode: str | None = None,
        record_telemetry: bool = True,
    ) -> dict[str, Any]:
        self._ensure_workspace()
        policy = retrieval_policy(mode, budget)
        query = f"{project or ''} {task}".strip()
        temporal = TemporalRetrievalRequest.resolve(
            task,
            valid_as_of,
            known_as_of=known_as_of,
            event_as_of=event_as_of,
            event_kind=event_kind,
            temporal_mode=temporal_mode,
        )
        if temporal.include_supporting_chunks:
            chunk_candidates, fanout_debug = self._fanout_chunk_candidates(
                query, limit=60
            )
        else:
            chunk_candidates = []
            fanout_debug = {
                "lexical": [],
                "vector": [],
                "vector_unavailable_reason": None,
                "fused": [],
                "candidate_ids": [],
                "temporal_omission": "knowledge_time",
            }
        lineage_scores = self._lineage_scores_for_chunks(chunk_candidates)
        reranked_chunks = rerank_chunks(
            query,
            chunk_candidates,
            fanout_debug,
            lineage_scores=lineage_scores,
            apply_recency=temporal.include_current_layers,
        )
        relevant_facts = self.search_facts(
            query,
            limit=min(8, policy.max_chunks),
            temporal_request=temporal,
        )
        supporting_chunks = (
            []
            if not temporal.include_supporting_chunks
            else select_context_chunks(
                suppress_chunks_covered_by_facts(reranked_chunks, relevant_facts),
                query=query,
                policy=policy,
            )
        )
        if temporal.include_current_layers:
            wiki_pages = self.select_wiki_pages(
                query, supporting_chunks, limit=policy.max_wiki_pages
            )
            memories = relevant_memories_for_query(
                self.active_memories(project),
                query,
                limit=policy.max_memories,
                score_floor=MEMORY_ACTIVE_SCORE_FLOOR,
            )
            candidate_memories = relevant_memories_for_query(
                self.candidate_memories(project),
                query,
                limit=min(policy.max_memories, 3),
                score_floor=MEMORY_CANDIDATE_SCORE_FLOOR,
            )
            open_questions = self.relevant_open_questions(query, limit=5)
        else:
            # These projections are current-state artifacts without version history.
            wiki_pages = []
            memories = []
            candidate_memories = []
            open_questions = []
        citation_snapshots = (
            fact_citation_snapshots(relevant_facts)
            + chunk_citation_snapshots(supporting_chunks)
            + wiki_page_citation_snapshots(wiki_pages)
        )
        assessment = retrieval_assessment(
            supporting_chunks,
            wiki_pages,
            memories,
            candidate_memories,
            relevant_facts,
        )
        event_id = None
        retrieval_debug = build_retrieval_debug(
            query,
            fanout_debug,
            supporting_chunks,
            reranked_chunks,
            wiki_pages,
            debug=debug,
        )
        retrieval_debug["assessment"] = assessment
        retrieval_debug["temporal"] = temporal.debug()
        if fanout_debug.get("vector_unavailable_reason"):
            assessment["reasons"].append(
                f"vector_search unavailable: {fanout_debug['vector_unavailable_reason']}"
            )
        if not self.read_only and record_telemetry:
            event_id = new_id("retrieval")
            with connection(self.paths.sqlite_path) as conn:
                conn.execute(
                    """
                    INSERT INTO retrieval_events(
                      id, query, timestamp, caller, returned_chunk_ids, selected_chunk_ids, citation_snapshots, debug
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event_id,
                        truncate_for_packet(query, RETRIEVAL_EVENT_QUERY_MAX_CHARS),
                        now_iso(),
                        "retrieve_context",
                        dumps(list(fanout_debug["fused"])[:RETRIEVAL_EVENT_ID_LIMIT]),
                        dumps(
                            [row["chunk_id"] for row in supporting_chunks][
                                :RETRIEVAL_EVENT_ID_LIMIT
                            ]
                        ),
                        dumps(
                            stored_citation_snapshots(citation_snapshots, debug=debug)
                        ),
                        stored_retrieval_debug(retrieval_debug) if debug else "{}",
                    ),
                )
                self._record_retrieval_exposures(
                    conn,
                    retrieval_event_id=event_id,
                    query=query,
                    supporting_chunks=supporting_chunks,
                    relevant_facts=relevant_facts,
                    wiki_pages=wiki_pages,
                    active_memories=memories,
                )
        result = {
            "task": task,
            "project": project,
            "budget": policy.total_budget,
            "retrieval_mode": policy.mode,
            "retrieval_verdict": assessment["verdict"],
            "retrieval_confidence": assessment["confidence"],
            "retrieval_reasons": assessment["reasons"],
            "temporal": temporal.envelope(),
            "active_memories": memories,
            "candidate_memories": candidate_memories,
            "relevant_wiki_pages": wiki_pages,
            "relevant_facts": relevant_facts,
            "supporting_chunks": supporting_chunks,
            "citation_snapshots": citation_snapshots,
            "open_questions": open_questions,
            "omitted_due_to_budget": [
                {
                    "chunk_id": row.get("chunk_id"),
                    "document_id": row.get("document_id"),
                    "source_type": row.get("source_type"),
                    "omitted_tokens": row.get("omitted_tokens", 0),
                }
                for row in supporting_chunks
                if int(row.get("omitted_tokens") or 0) > 0
            ],
            "retrieval_event_id": event_id,
        }
        if debug:
            result["retrieval_policy"] = {
                "total_budget": policy.total_budget,
                "max_chunks": policy.max_chunks,
                "max_wiki_pages": policy.max_wiki_pages,
                "max_memories": policy.max_memories,
                "default_chunk_cap": policy.default_chunk_cap,
                "source_caps": policy.source_caps,
                "include_full_text": policy.include_full_text,
            }
            result["retrieval_debug"] = retrieval_debug
        else:
            result = slim_retrieve_context_packet(result, policy)
        return result

    def relevant_open_questions(
        self, query: str, limit: int = 5
    ) -> list[dict[str, Any]]:
        terms = important_query_terms(query)
        specific_terms = specific_query_terms(query)
        if not terms or limit <= 0:
            return []
        with connection(self.paths.sqlite_path) as conn:
            candidates = rows(
                conn,
                """
                SELECT *
                FROM open_questions
                WHERE status IN ('open', 'needs_human')
                ORDER BY created_at DESC
                LIMIT 100
                """,
            )
        scored: list[dict[str, Any]] = []
        for row in candidates:
            question = open_question_row_to_context(row)
            haystack = " ".join(
                [
                    str(question.get("kind") or ""),
                    str(question.get("entity_key") or ""),
                    str(question.get("page_hint") or ""),
                    str(question.get("question") or ""),
                    json.dumps(question.get("context") or {}, sort_keys=True),
                    json.dumps(
                        question.get("recommended_action") or {}, sort_keys=True
                    ),
                    " ".join(
                        str(fact_id) for fact_id in question.get("fact_ids") or []
                    ),
                ]
            )
            matches = terms_in_text(terms, haystack)
            specific_matches = terms_in_text(specific_terms, haystack)
            if not matches:
                continue
            score = len(matches) + len(specific_matches)
            if specific_terms and not specific_matches and score < 2:
                continue
            question["question_relevance_score"] = score
            question["matched_query_terms"] = matches
            question["matched_specific_query_terms"] = specific_matches
            scored.append(question)
        scored.sort(
            key=lambda item: (
                -int(item.get("question_relevance_score") or 0),
                _descending_text_sort_key(str(item.get("created_at") or "")),
                str(item.get("id") or ""),
            )
        )
        return scored[:limit]

    def record_context_feedback(
        self,
        target_type: str,
        target_id: str,
        useful: bool,
        note: str | None = None,
    ) -> dict[str, Any]:
        self._ensure_writable()
        self.init_workspace()
        normalized_type, normalized_id = normalize_lineage_target(
            target_type, target_id
        )
        event_type = "explicit_useful" if useful else "explicit_not_useful"
        weight = LINEAGE_EVENT_WEIGHTS[event_type]
        metadata = {"note": note} if note else {}
        event_id = new_id("lineage")
        created_at = now_iso()
        with connection(self.paths.sqlite_path) as conn:
            insert_context_lineage_event(
                conn,
                event_id=event_id,
                target_type=normalized_type,
                target_id=normalized_id,
                event_type=event_type,
                retrieval_event_id=None,
                agent_session_id=None,
                query=None,
                weight=weight,
                metadata=metadata,
                created_at=created_at,
            )
        return {
            "event_id": event_id,
            "target_type": normalized_type,
            "target_id": normalized_id,
            "event_type": event_type,
            "weight": weight,
            "created_at": created_at,
        }

    def _lineage_scores_for_chunks(
        self, chunks: list[dict[str, Any]]
    ) -> dict[str, dict[str, Any]]:
        chunk_ids = sorted(
            {
                str(chunk.get("chunk_id") or "")
                for chunk in chunks
                if chunk.get("chunk_id")
            }
        )
        document_ids = sorted(
            {
                str(chunk.get("document_id") or "")
                for chunk in chunks
                if chunk.get("document_id")
            }
        )
        if not chunk_ids and not document_ids:
            return {}

        clauses: list[str] = []
        params: list[Any] = []
        if chunk_ids:
            clauses.append(
                f"(target_type = 'chunk' AND target_id IN ({','.join('?' for _ in chunk_ids)}))"
            )
            params.extend(chunk_ids)
        if document_ids:
            clauses.append(
                f"(target_type = 'document' AND target_id IN ({','.join('?' for _ in document_ids)}))"
            )
            params.extend(document_ids)
        with connection(self.paths.sqlite_path) as conn:
            lineage_rows = rows(
                conn,
                f"""
                SELECT *
                FROM context_lineage_events
                WHERE {" OR ".join(clauses)}
                ORDER BY created_at DESC
                """,
                params,
            )

        chunk_ids_by_document: dict[str, list[str]] = {}
        for chunk in chunks:
            document_id = str(chunk.get("document_id") or "")
            chunk_id = str(chunk.get("chunk_id") or "")
            if document_id and chunk_id:
                chunk_ids_by_document.setdefault(document_id, []).append(chunk_id)

        score_by_chunk: dict[str, float] = {chunk_id: 0.0 for chunk_id in chunk_ids}
        reasons_by_chunk: dict[str, dict[str, float | int]] = {
            chunk_id: {} for chunk_id in chunk_ids
        }
        seen_agent_references: set[tuple[str, str, str]] = set()
        now = datetime.now(timezone.utc)

        for lineage in lineage_rows:
            event_type = str(lineage["event_type"])
            target_type = str(lineage["target_type"])
            target_id = str(lineage["target_id"])
            if event_type == "agent_referenced_id":
                agent_session_id = str(lineage["agent_session_id"] or "")
                dedupe_key = (target_type, target_id, agent_session_id)
                if agent_session_id and dedupe_key in seen_agent_references:
                    continue
                if agent_session_id:
                    seen_agent_references.add(dedupe_key)

            base_weight = float(
                lineage["weight"] or LINEAGE_EVENT_WEIGHTS.get(event_type, 0.0)
            )
            if base_weight == 0.0:
                continue
            decay = lineage_decay(str(lineage["created_at"]), now)
            weighted = base_weight * decay
            affected_chunk_ids = (
                [target_id]
                if target_type == "chunk"
                else chunk_ids_by_document.get(target_id, [])
            )
            for chunk_id in affected_chunk_ids:
                if chunk_id not in score_by_chunk:
                    continue
                score_by_chunk[chunk_id] += weighted
                reason_counts = reasons_by_chunk[chunk_id]
                reason_counts[event_type] = (
                    float(reason_counts.get(event_type, 0.0)) + weighted
                )
                reason_counts[f"{event_type}:count"] = (
                    int(reason_counts.get(f"{event_type}:count", 0)) + 1
                )

        output: dict[str, dict[str, Any]] = {}
        for chunk_id, score in score_by_chunk.items():
            capped = max(-LINEAGE_MAX_ABS_BOOST, min(LINEAGE_MAX_ABS_BOOST, score))
            if capped == 0.0:
                continue
            output[chunk_id] = {
                "boost": round(capped, 4),
                "reasons": lineage_reason_strings(
                    reasons_by_chunk.get(chunk_id, {}), capped
                ),
            }
        return output

    def _record_retrieval_exposures(
        self,
        conn: Any,
        retrieval_event_id: str,
        query: str,
        supporting_chunks: list[dict[str, Any]],
        relevant_facts: list[dict[str, Any]],
        wiki_pages: list[dict[str, Any]],
        active_memories: list[dict[str, Any]],
    ) -> None:
        created_at = now_iso()
        for fact in relevant_facts:
            fact_id = str(fact.get("id") or "")
            if not fact_id:
                continue
            insert_context_lineage_event(
                conn,
                event_id=new_id("lineage"),
                target_type="fact",
                target_id=fact_id,
                event_type="exposed",
                retrieval_event_id=retrieval_event_id,
                agent_session_id=None,
                query=query,
                weight=0.0,
                metadata={
                    "page_hint": fact.get("page_hint"),
                    "status": fact.get("status"),
                    "conflict_group_id": fact.get("conflict_group_id"),
                },
                created_at=created_at,
            )
        for chunk in supporting_chunks:
            chunk_id = str(chunk.get("chunk_id") or "")
            if not chunk_id:
                continue
            insert_context_lineage_event(
                conn,
                event_id=new_id("lineage"),
                target_type="chunk",
                target_id=chunk_id,
                event_type="exposed",
                retrieval_event_id=retrieval_event_id,
                agent_session_id=None,
                query=query,
                weight=0.0,
                metadata={
                    "document_id": chunk.get("document_id"),
                    "source_type": chunk.get("source_type"),
                },
                created_at=created_at,
            )
        for page in wiki_pages:
            target_id = str(page.get("relative_path") or page.get("path") or "")
            if not target_id:
                continue
            insert_context_lineage_event(
                conn,
                event_id=new_id("lineage"),
                target_type="wiki_page",
                target_id=target_id,
                event_type="exposed",
                retrieval_event_id=retrieval_event_id,
                agent_session_id=None,
                query=query,
                weight=0.0,
                metadata={
                    "title": page.get("title"),
                    "page_type": page.get("page_type"),
                },
                created_at=created_at,
            )
        for memory in active_memories:
            memory_id = str(memory.get("id") or "")
            if not memory_id:
                continue
            insert_context_lineage_event(
                conn,
                event_id=new_id("lineage"),
                target_type="memory",
                target_id=memory_id,
                event_type="exposed",
                retrieval_event_id=retrieval_event_id,
                agent_session_id=None,
                query=query,
                weight=0.0,
                metadata={
                    "memory_type": memory.get("memory_type"),
                    "scope": memory.get("scope"),
                },
                created_at=created_at,
            )

    def _fanout_chunk_candidates(self, query: str, limit: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        raw_limit = min(1000, max(240, limit * 16))
        lexical = self._search_fts(query, raw_limit)
        vector_unavailable_reason = None
        try:
            vector = search_vectors(
                self.paths.lancedb_path, self.embedding_provider, query, raw_limit
            )
        except (EmbeddingProviderUnavailable, VectorIndexUnavailable) as exc:
            vector = []
            vector_unavailable_reason = str(exc)
        vector_debug = [
            {
                "chunk_id": row.get("chunk_id"),
                "document_id": row.get("document_id"),
                "distance": row.get("_distance"),
                "preview": str(row.get("text", ""))[:160],
            }
            for row in vector
        ]
        lexical_ids = [row["chunk_id"] for row in lexical]
        vector_ids = [row["chunk_id"] for row in vector if row.get("chunk_id")]
        fused_ids = reciprocal_rank_fusion(lexical_ids, vector_ids)
        candidate_ids = dedupe_preserve_order(fused_ids + lexical_ids + vector_ids)
        candidates = self._chunks_by_ids(candidate_ids)
        candidates = [row for row in candidates if not gmail_retrieval_noise_reasons(row, query)][:limit]
        candidate_ids = [row["chunk_id"] for row in candidates]
        return candidates, {
            "lexical": lexical,
            "vector": vector_debug,
            "vector_unavailable_reason": vector_unavailable_reason,
            "fused": fused_ids,
            "candidate_ids": candidate_ids,
        }

    def search_wiki_pages(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        self.init_workspace()
        terms = query_terms(query)
        if not terms or not self.paths.wiki.exists():
            return []
        results: list[dict[str, Any]] = []
        from .wiki import parse_frontmatter

        for path in sorted(self.paths.wiki.rglob("*.md")):
            text = path.read_text(encoding="utf-8", errors="replace")
            frontmatter, body = parse_frontmatter(text)
            if frontmatter is None:
                continue
            page_type = str(frontmatter.get("page_type") or "")
            haystack = " ".join(
                [
                    str(frontmatter.get("title") or ""),
                    page_type,
                    " ".join(frontmatter.get("tags") or []),
                    body,
                ]
            ).lower()
            score = sum(1 for term in terms if term in haystack)
            if score <= 0:
                continue
            if page_type not in {"index", "reference"}:
                score += 3
            elif page_type == "index":
                score += 1
            summary = first_section(body, "Summary")
            source_ids = frontmatter.get("source_ids") or []
            results.append(
                {
                    "title": frontmatter.get("title") or path.stem,
                    "page_type": page_type,
                    "path": str(path),
                    "relative_path": str(path.relative_to(self.paths.wiki)),
                    "source_ids": source_ids[:8],
                    "source_count": len(source_ids),
                    "summary": summary,
                    "score": score,
                }
            )
        results.sort(
            key=lambda page: (
                -page["score"],
                page["page_type"] == "reference",
                page["title"],
            )
        )
        return results[:limit]

    def search_facts(
        self,
        query: str,
        limit: int = 8,
        *,
        valid_as_of: str | None = None,
        temporal_request: TemporalRetrievalRequest | None = None,
    ) -> list[dict[str, Any]]:
        self.init_workspace()
        fts_query = build_fts_query(query)
        if not fts_query or limit <= 0:
            return []
        if temporal_request is not None and valid_as_of is not None:
            raise ValueError(
                "temporal_request and valid_as_of cannot both be provided"
            )
        temporal = temporal_request or TemporalRetrievalRequest.resolve(
            query, valid_as_of
        )
        with connection(self.paths.sqlite_path) as conn:
            return search_temporal_facts(
                conn,
                fts_query=fts_query,
                query=query,
                limit=limit,
                request=temporal,
                truth_confidence_floor=FACT_TRUTH_CONFIDENCE_FLOOR,
                row_to_retrieval_fact=row_to_retrieval_fact,
                score_fact=score_retrieval_fact_for_query,
                cut_facts=dynamic_fact_cut,
            )

    def select_wiki_pages(
        self,
        query: str,
        selected_chunks: list[dict[str, Any]],
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        self.init_workspace()
        terms = important_query_terms(query)
        specific_terms = specific_query_terms(query)
        required_specific_hits = required_specific_hit_count(specific_terms)
        agent_query = is_agent_query(terms)
        if not terms or not self.paths.wiki.exists():
            return []

        selected_sources = {
            f"document:{row['document_id']}"
            for row in selected_chunks
            if row.get("document_id")
        } | {row["chunk_id"] for row in selected_chunks if row.get("chunk_id")}

        from .wiki import parse_frontmatter

        results: list[dict[str, Any]] = []
        for path in sorted(self.paths.wiki.rglob("*.md")):
            text = path.read_text(encoding="utf-8", errors="replace")
            frontmatter, body = parse_frontmatter(text)
            if frontmatter is None:
                continue

            title = str(frontmatter.get("title") or path.stem)
            page_type = str(frontmatter.get("page_type") or "")
            status = str(frontmatter.get("status") or "active")
            source_ids = list(frontmatter.get("source_ids") or [])
            relative_path = str(path.relative_to(self.paths.wiki))
            is_agent_reference = "agent_session_log" in relative_path
            tags = list(frontmatter.get("tags") or [])
            managed = (
                bool(frontmatter.get("managed"))
                or "managed" in tags
                or str(frontmatter.get("id") or "").startswith("managed-")
            )
            semantic_page = page_type not in {"index", "reference"}
            if status == "archived":
                continue
            title_haystack = " ".join(
                [
                    title,
                    page_type,
                    relative_path,
                    " ".join(tags),
                ]
            ).lower()
            body_haystack = body.lower()
            title_hits = terms_in_text(terms, title_haystack)
            body_hits = terms_in_text(terms, body_haystack)
            specific_hits = sorted(
                set(terms_in_text(specific_terms, title_haystack)).union(
                    terms_in_text(specific_terms, body_haystack)
                )
            )
            source_overlap = sorted(selected_sources.intersection(source_ids))

            if (
                not title_hits
                and not (page_type == "reference" and source_overlap)
                and len(body_hits) < 2
            ):
                continue
            if is_agent_reference and not agent_query and not source_overlap:
                continue
            if (
                required_specific_hits
                and len(specific_hits) < required_specific_hits
                and not source_overlap
            ):
                continue

            score = 0.0
            reasons: list[str] = []
            if title_hits:
                boost = 5.0 * len(title_hits)
                score += boost
                reasons.append(
                    f"title/path matched {', '.join(title_hits)} (+{boost:g})"
                )
            if body_hits:
                boost = float(min(len(body_hits), 6))
                score += boost
                reasons.append(f"body matched {', '.join(body_hits[:6])} (+{boost:g})")
            if source_overlap:
                boost = 4.0 if page_type == "reference" else 6.0
                boost += min(6.0, float(len(source_overlap) * 2))
                score += boost
                reasons.append(f"shares selected source evidence (+{boost:g})")
            if managed:
                score += MANAGED_WIKI_BOOST
                reasons.append(f"managed chief-of-staff page (+{MANAGED_WIKI_BOOST:g})")
            elif semantic_page:
                score += SEMANTIC_WIKI_BOOST
                reasons.append(f"semantic wiki page (+{SEMANTIC_WIKI_BOOST:g})")
            if page_type == "reference":
                score -= 1.0
                reasons.append("reference page penalty (-1)")
            elif page_type == "index":
                score -= 4.0
                reasons.append("index page penalty (-4)")
            if is_agent_reference and not agent_query:
                score -= 16.0
                reasons.append("agent-log reference penalty (-16)")
            if score < WIKI_PAGE_SCORE_FLOOR:
                continue

            results.append(
                {
                    "title": title,
                    "page_type": page_type,
                    "path": str(path),
                    "relative_path": relative_path,
                    "source_ids": source_ids[:8],
                    "source_count": len(source_ids),
                    "summary": first_section(body, "Summary"),
                    "score": round(score, 4),
                    "selection_reasons": reasons,
                    "managed": managed,
                    "query_specific_hits": specific_hits,
                }
            )

        results.sort(
            key=lambda page: (
                -page["score"],
                not page.get("managed", False),
                page["page_type"] == "reference",
                page["title"],
            )
        )
        return results[:limit]

    def active_memories(self, project: str | None = None) -> list[dict[str, Any]]:
        return self.list_memories(status="active", project=project)

    def candidate_memories(self, project: str | None = None) -> list[dict[str, Any]]:
        return self.list_memories(status="proposed", project=project)

    def list_memories(
        self,
        status: str | None = None,
        scope: str | None = None,
        memory_type: str | None = None,
        project: str | None = None,
    ) -> list[dict[str, Any]]:
        self._ensure_workspace()
        with connection(self.paths.sqlite_path) as conn:
            query = "SELECT * FROM memories WHERE 1=1"
            params: list[Any] = []
            if status:
                query += " AND status = ?"
                params.append(status)
            if scope:
                query += " AND scope = ?"
                params.append(scope)
            if project:
                query += " AND (scope = ? OR scope = 'global')"
                params.append(f"project:{project}")
            if memory_type:
                query += " AND memory_type = ?"
                params.append(memory_type)
            query += " ORDER BY updated_at DESC, created_at DESC"
            return [row_to_memory(row) for row in conn.execute(query, params)]

    def get_memory(self, memory_id: str) -> dict[str, Any]:
        self._ensure_workspace()
        with connection(self.paths.sqlite_path) as conn:
            row = conn.execute(
                "SELECT * FROM memories WHERE id = ?", (memory_id,)
            ).fetchone()
        if not row:
            raise ValueError(f"memory not found: {memory_id}")
        return row_to_memory(row)

    def propose_memory(
        self,
        memory_type: str,
        scope: str,
        content: str,
        sources: list[str],
        confidence: float,
    ) -> str:
        self._ensure_writable()
        self.init_workspace()
        normalized_scope = scope.strip()
        if not valid_memory_scope(normalized_scope):
            raise ValueError(
                "invalid memory scope "
                f"{scope!r}; expected 'global' or a non-empty scope prefixed by "
                "project:, repo:, agent:, topic:, or user:"
            )
        memory_id = new_id("mem")
        timestamp = now_iso()
        with connection(self.paths.sqlite_path) as conn:
            conn.execute(
                """
                INSERT INTO memories(id, memory_type, scope, content, confidence, source_ids, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    memory_id,
                    memory_type,
                    normalized_scope,
                    content,
                    confidence,
                    dumps(sources),
                    "proposed",
                    timestamp,
                    timestamp,
                ),
            )
        return memory_id

    def approve_memory(self, memory_id: str) -> dict[str, Any]:
        return self._set_memory_status(memory_id, "active")

    def reject_memory(self, memory_id: str, reason: str) -> dict[str, Any]:
        if not reason.strip():
            raise ValueError("reject reason is required")
        return self._set_memory_status(memory_id, "rejected", reason=reason.strip())

    def archive_memory(self, memory_id: str) -> dict[str, Any]:
        return self._set_memory_status(memory_id, "archived")

    def export_all_memories(self) -> dict[str, Any]:
        self.init_workspace()
        written: list[str] = []
        removed: list[str] = []
        with connection(self.paths.sqlite_path) as conn:
            all_memories = [
                row_to_memory(row)
                for row in conn.execute("SELECT * FROM memories ORDER BY id")
            ]
        active_ids = {
            memory["id"] for memory in all_memories if memory["status"] == "active"
        }
        active_paths = {
            memory["id"]: memory_export_path(self.paths, memory)
            for memory in all_memories
            if memory["status"] == "active"
        }
        for memory in all_memories:
            if memory["status"] == "active":
                written.append(str(write_memory_export(self.paths, memory)))
            else:
                path = memory_export_path(self.paths, memory)
                if path.exists():
                    path.unlink()
                    removed.append(str(path))
        for path in (
            self.paths.memory.rglob("*.md") if self.paths.memory.exists() else []
        ):
            if path.stem.startswith("mem_") and (
                path.stem not in active_ids or path != active_paths.get(path.stem)
            ):
                path.unlink()
                removed.append(str(path))
        return {"written": written, "removed": sorted(set(removed))}

    def import_memories(
        self, source_dir: Path, allow_missing_sources: bool = False
    ) -> dict[str, Any]:
        self.init_workspace()
        source_dir = source_dir.expanduser().resolve()
        imported: list[str] = []
        errors: list[str] = []
        if not source_dir.exists():
            raise ValueError(f"memory import directory not found: {source_dir}")
        with connection(self.paths.sqlite_path) as conn:
            document_ids = {
                row["id"] for row in conn.execute("SELECT id FROM documents")
            }
            for path in sorted(source_dir.rglob("*.md")):
                try:
                    memory = read_memory_export(path)
                    missing = [
                        source_id
                        for source_id in memory["source_ids"]
                        if source_id.startswith("document:")
                        and source_id.removeprefix("document:") not in document_ids
                    ]
                    if missing and not allow_missing_sources:
                        errors.append(
                            f"{path}: missing source documents: {', '.join(missing)}"
                        )
                        continue
                    timestamp = now_iso()
                    conn.execute(
                        """
                        INSERT INTO memories(
                          id, memory_type, scope, content, confidence, source_ids,
                          status, created_at, updated_at, last_seen_at, reviewed_at, review_reason
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(id) DO UPDATE SET
                          memory_type = excluded.memory_type,
                          scope = excluded.scope,
                          content = excluded.content,
                          confidence = excluded.confidence,
                          source_ids = excluded.source_ids,
                          status = excluded.status,
                          updated_at = excluded.updated_at,
                          last_seen_at = excluded.last_seen_at,
                          reviewed_at = excluded.reviewed_at,
                          review_reason = excluded.review_reason
                        """,
                        (
                            memory["id"],
                            memory["memory_type"],
                            memory["scope"],
                            memory["content"],
                            memory["confidence"],
                            dumps(memory["source_ids"]),
                            memory["status"],
                            memory.get("created_at") or timestamp,
                            timestamp,
                            memory.get("last_seen_at"),
                            memory.get("reviewed_at"),
                            memory.get("review_reason"),
                        ),
                    )
                    imported.append(memory["id"])
                except Exception as exc:
                    errors.append(f"{path}: {exc}")
        return {"imported": imported, "errors": errors}

    def _set_memory_status(
        self, memory_id: str, status: str, reason: str | None = None
    ) -> dict[str, Any]:
        timestamp = now_iso()
        with connection(self.paths.sqlite_path) as conn:
            result = conn.execute(
                """
                UPDATE memories
                SET status = ?, updated_at = ?, reviewed_at = ?, review_reason = ?
                WHERE id = ?
                """,
                (status, timestamp, timestamp, reason, memory_id),
            )
            if result.rowcount == 0:
                raise ValueError(f"memory not found: {memory_id}")
            row = conn.execute(
                "SELECT * FROM memories WHERE id = ?", (memory_id,)
            ).fetchone()
        if not row:
            raise ValueError(f"memory not found: {memory_id}")
        memory = row_to_memory(row)
        if status == "active":
            write_memory_export(self.paths, memory)
        else:
            path = memory_export_path(self.paths, memory)
            if path.exists():
                path.unlink()
        return memory

    def write_agent_session(
        self,
        summary: str,
        files_touched: list[str],
        commands_run: list[str],
        outcome: str,
        unresolved_issues: list[str],
    ) -> str:
        self._ensure_writable()
        self.init_workspace()
        session_id = new_id("session")
        with connection(self.paths.sqlite_path) as conn:
            conn.execute(
                """
                INSERT INTO agent_sessions(id, summary, files_touched, commands_run, outcome, unresolved_issues, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    summary,
                    dumps(files_touched),
                    dumps(commands_run),
                    outcome,
                    dumps(unresolved_issues),
                    now_iso(),
                ),
            )
        return session_id

    def _search_fts(self, query: str, limit: int) -> list[dict[str, Any]]:
        fts_query = build_fts_query(query)
        if not fts_query:
            return []
        with connection(self.paths.sqlite_path) as conn:
            if sqlite_table_exists(conn, "retrieval_fts"):
                found = rows(
                    conn,
                    """
                    SELECT retrieval_fts.target_id AS chunk_id,
                           bm25(retrieval_fts) AS score
                    FROM retrieval_fts
                    JOIN chunks c ON c.id = retrieval_fts.target_id
                    JOIN documents d ON d.id = c.document_id
                    WHERE retrieval_fts MATCH ?
                      AND retrieval_fts.kind = 'chunk'
                      AND d.status = 'active'
                    ORDER BY score
                    LIMIT ?
                    """,
                    (fts_query, limit),
                )
                if found:
                    return [dict(row) for row in found]
            found = rows(
                conn,
                """
                SELECT chunk_fts.chunk_id, bm25(chunk_fts) AS score
                FROM chunk_fts
                JOIN chunks c ON c.id = chunk_fts.chunk_id
                JOIN documents d ON d.id = c.document_id
                WHERE chunk_fts MATCH ?
                  AND d.status = 'active'
                ORDER BY score
                LIMIT ?
                """,
                (fts_query, limit),
            )
            return [dict(row) for row in found]

    def _chunks_by_ids(self, chunk_ids: list[str]) -> list[dict[str, Any]]:
        if not chunk_ids:
            return []
        placeholders = ",".join("?" for _ in chunk_ids)
        with connection(self.paths.sqlite_path) as conn:
            found = rows(
                conn,
                f"""
                SELECT c.id AS chunk_id, c.text, c.heading_path, c.chunk_index, c.token_count,
                       c.content_hash, c.created_at AS chunk_created_at,
                       d.id AS document_id, d.title, d.source_type, d.source_path, d.raw_path,
                       d.origin_node_id, d.logical_source_key, d.tags,
                       d.created_at AS document_created_at, d.ingested_at AS document_ingested_at
                FROM chunks c
                JOIN documents d ON d.id = c.document_id
                WHERE c.id IN ({placeholders})
                  AND d.status = 'active'
                """,
                chunk_ids,
            )
        by_id = {row["chunk_id"]: dict(row) for row in found}
        return [by_id[chunk_id] for chunk_id in chunk_ids if chunk_id in by_id]


def reserved_ingest_path(path: Path) -> bool:
    reserved = {"_staging", "_quarantine", "_rejected", ".rsync-partial"}
    return bool(set(path.parts).intersection(reserved)) or path.name.endswith(
        ".error.json"
    )


def external_inbox_path_info(paths: BrainPaths, path: Path) -> tuple[Path, Path] | None:
    external_root = paths.inbox / "external"
    try:
        relative = path.resolve().relative_to(external_root.resolve())
    except ValueError:
        return None
    if len(relative.parts) < 2:
        return None
    peer_root = external_root / relative.parts[0]
    return peer_root, Path(*relative.parts[1:])


def quarantine_error_path(quarantine_root: Path, relative_path: Path) -> Path:
    return quarantine_root / Path(f"{relative_path.as_posix()}.error.json")


def remove_empty_dirs(root: Path) -> None:
    if not root.exists():
        return
    for path in sorted(root.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        if path.is_dir():
            try:
                path.rmdir()
            except OSError:
                pass
    try:
        root.rmdir()
    except OSError:
        pass


def row_to_memory(row: Any) -> dict[str, Any]:
    output = dict(row)
    output["source_ids"] = loads(output.get("source_ids"), [])
    return output


def memory_scope_dir(scope: str) -> str:
    cleaned = scope.strip().replace("/", "_")
    return cleaned or "global"


def memory_export_path(paths: BrainPaths, memory: dict[str, Any]) -> Path:
    return paths.memory / memory_scope_dir(str(memory["scope"])) / f"{memory['id']}.md"


def write_memory_export(paths: BrainPaths, memory: dict[str, Any]) -> Path:
    path = memory_export_path(paths, memory)
    path.parent.mkdir(parents=True, exist_ok=True)
    frontmatter = {
        "id": memory["id"],
        "memory_type": memory["memory_type"],
        "scope": memory["scope"],
        "confidence": memory["confidence"],
        "source_ids": memory.get("source_ids", []),
        "reviewed_at": memory.get("reviewed_at"),
        "reviewed_by": "brain",
        "status": memory["status"],
        "created_at": memory.get("created_at"),
        "updated_at": memory.get("updated_at"),
        "last_seen_at": memory.get("last_seen_at"),
        "review_reason": memory.get("review_reason"),
    }
    serialized = yaml.safe_dump(
        frontmatter, sort_keys=True, allow_unicode=False
    ).strip()
    path.write_text(
        f"---\n{serialized}\n---\n\n{memory['content'].rstrip()}\n", encoding="utf-8"
    )
    return path


def read_memory_export(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        raise ValueError("missing YAML frontmatter")
    end = text.find("\n---", 4)
    if end == -1:
        raise ValueError("unterminated YAML frontmatter")
    frontmatter = yaml.safe_load(text[4:end]) or {}
    content = text[end + 4 :].lstrip("\n")
    memory_id = str(frontmatter.get("id") or path.stem)
    source_ids = frontmatter.get("source_ids") or []
    if not isinstance(source_ids, list):
        raise ValueError("source_ids must be a list")
    required = ["memory_type", "scope", "confidence", "status"]
    missing = [key for key in required if frontmatter.get(key) in (None, "")]
    if missing:
        raise ValueError(f"missing frontmatter keys: {', '.join(missing)}")
    return {
        "id": memory_id,
        "memory_type": str(frontmatter["memory_type"]),
        "scope": str(frontmatter["scope"]),
        "content": content.rstrip(),
        "confidence": float(frontmatter["confidence"]),
        "source_ids": [str(source_id) for source_id in source_ids],
        "status": str(frontmatter["status"]),
        "created_at": frontmatter.get("created_at"),
        "updated_at": frontmatter.get("updated_at"),
        "last_seen_at": frontmatter.get("last_seen_at"),
        "reviewed_at": frontmatter.get("reviewed_at"),
        "review_reason": frontmatter.get("review_reason"),
    }


def retrieval_policy(mode: str, budget: int | None = None) -> RetrievalPolicy:
    normalized = mode.strip().lower() if mode else DEFAULT_RETRIEVAL_MODE
    if normalized not in VALID_RETRIEVAL_MODES:
        raise ValueError(f"invalid retrieval mode: {mode}")
    presets = {
        "compact": RetrievalPolicy(
            mode="compact",
            total_budget=2500,
            max_chunks=4,
            max_wiki_pages=4,
            max_memories=6,
            default_chunk_cap=900,
            source_caps={
                "agent_session_log": 600,
                "gmail_thread": 900,
                "hyprnote_meeting": 900,
                "markdown_note": 1000,
            },
        ),
        "default": RetrievalPolicy(
            mode="default",
            total_budget=8000,
            max_chunks=8,
            max_wiki_pages=8,
            max_memories=8,
            default_chunk_cap=1400,
            source_caps={
                "agent_session_log": 1000,
                "gmail_thread": 1400,
                "hyprnote_meeting": 1400,
                "markdown_note": 1800,
            },
        ),
        "broad": RetrievalPolicy(
            mode="broad",
            total_budget=12000,
            max_chunks=8,
            max_wiki_pages=8,
            max_memories=12,
            default_chunk_cap=2200,
            source_caps={
                "agent_session_log": 1800,
                "gmail_thread": 2200,
                "hyprnote_meeting": 2500,
                "markdown_note": 3000,
            },
        ),
        "inspect": RetrievalPolicy(
            mode="inspect",
            total_budget=16000,
            max_chunks=8,
            max_wiki_pages=8,
            max_memories=12,
            default_chunk_cap=16000,
            source_caps={
                "agent_session_log": 16000,
                "gmail_thread": 16000,
                "hyprnote_meeting": 16000,
                "markdown_note": 16000,
            },
            include_full_text=True,
        ),
    }
    policy = presets[normalized]
    if budget is None:
        return policy
    return RetrievalPolicy(
        mode=policy.mode,
        total_budget=max(1, budget),
        max_chunks=policy.max_chunks,
        max_wiki_pages=policy.max_wiki_pages,
        max_memories=policy.max_memories,
        default_chunk_cap=policy.default_chunk_cap,
        source_caps=policy.source_caps,
        include_full_text=policy.include_full_text,
    )


def rerank_chunks(
    query: str,
    chunks: list[dict[str, Any]],
    fanout_debug: dict[str, Any],
    lineage_scores: dict[str, dict[str, Any]] | None = None,
    *,
    apply_recency: bool = True,
) -> list[dict[str, Any]]:
    terms = important_query_terms(query)
    specific_terms = specific_query_terms(query)
    required_specific_hits = required_specific_hit_count(specific_terms)
    agent_query = is_agent_query(terms)
    anchors = anchor_query_terms(terms, chunks, agent_query=agent_query)
    recency_intent = has_recency_intent(query, terms)
    lineage_scores = lineage_scores or {}
    lexical_rank = {
        row["chunk_id"]: rank
        for rank, row in enumerate(fanout_debug.get("lexical", []), start=1)
        if row.get("chunk_id")
    }
    vector_rank = {
        row["chunk_id"]: rank
        for rank, row in enumerate(fanout_debug.get("vector", []), start=1)
        if row.get("chunk_id")
    }

    scored: list[dict[str, Any]] = []
    for row in chunks:
        candidate = dict(row)
        chunk_id = str(candidate.get("chunk_id") or "")
        title = str(candidate.get("title") or "").lower()
        heading = str(candidate.get("heading_path") or "").lower()
        text = str(candidate.get("text") or "")
        text_lower = text.lower()
        score = 0.0
        reasons: list[str] = []
        suppressed = False
        suppress_reasons: list[str] = []

        if chunk_id in lexical_rank:
            boost = 10.0 / (lexical_rank[chunk_id] ** 0.5)
            score += boost
            reasons.append(f"BM25 rank {lexical_rank[chunk_id]} (+{boost:.2f})")
        if chunk_id in vector_rank:
            boost = 8.0 / (vector_rank[chunk_id] ** 0.5)
            score += boost
            reasons.append(f"vector rank {vector_rank[chunk_id]} (+{boost:.2f})")

        title_hits = terms_in_text(terms, title)
        heading_hits = terms_in_text(terms, heading)
        text_hits = terms_in_text(terms, text_lower)
        source_type = str(candidate.get("source_type") or "")
        if source_type == "agent_session_log" and not agent_query:
            title_boost_hits = sorted(
                {
                    term
                    for term in title_hits
                    if term in heading_hits or term in text_hits
                }
            )
        else:
            title_boost_hits = title_hits
        anchor_hits = sorted(
            {
                term
                for term in anchors
                if contains_query_term(title, term)
                or contains_query_term(heading, term)
                or contains_query_term(text_lower, term)
            }
        )
        title_heading_anchor_hits = sorted(
            {
                term
                for term in anchors
                if contains_query_term(title, term)
                or contains_query_term(heading, term)
            }
        )
        local_term_hits = sorted(set(heading_hits).union(text_hits))
        local_specific_hits = sorted(
            {term for term in specific_terms if term in local_term_hits}
        )
        if title_boost_hits:
            boost = 4.0 * len(title_boost_hits)
            score += boost
            reasons.append(f"title matched {', '.join(title_boost_hits)} (+{boost:g})")
        ignored_title_hits = sorted(set(title_hits) - set(title_boost_hits))
        if ignored_title_hits:
            reasons.append(
                f"title-only matches ignored {', '.join(ignored_title_hits)}"
            )
        if heading_hits:
            boost = 2.5 * len(heading_hits)
            score += boost
            reasons.append(f"heading matched {', '.join(heading_hits)} (+{boost:g})")
        if text_hits and terms:
            boost = 6.0 * (len(text_hits) / len(terms))
            score += boost
            reasons.append(
                f"text covered {len(text_hits)}/{len(terms)} important terms (+{boost:.2f})"
            )
        if anchors:
            if anchor_hits:
                boost = 3.0 * len(anchor_hits)
                score += boost
                reasons.append(
                    f"entity anchor matched {', '.join(anchor_hits)} (+{boost:g})"
                )
                if not title_heading_anchor_hits:
                    score -= 6.0
                    reasons.append(
                        f"entity anchor absent from title/heading {', '.join(anchors)} (-6)"
                    )
            else:
                score -= 8.0
                reasons.append(f"missed entity anchor {', '.join(anchors)} (-8)")

        source_weight = SOURCE_TYPE_WEIGHTS.get(source_type, 0.0)
        if source_type == "agent_session_log" and agent_query:
            source_weight = 1.5
        if source_weight:
            score += source_weight
            reasons.append(f"source_type {source_type} ({source_weight:+g})")

        noise_reasons = chunk_noise_reasons(candidate)
        noise_reasons.extend(gmail_retrieval_noise_reasons(candidate, query))
        if retrieval_negative_control_fixture_mention(
            text_lower, terms, local_specific_hits
        ):
            noise_reasons.append("retrieval negative-control fixture")
            noise_reasons = dedupe_preserve_order(noise_reasons)
        if noise_reasons:
            if agent_query:
                penalty = 2.0
                score -= penalty
                reasons.append(f"minor noise penalty ({-penalty:g})")
            else:
                strong_noise = [
                    reason
                    for reason in noise_reasons
                    if reason != "raw transcript chunk"
                ]
                penalty = 4.0 if not strong_noise else 12.0 + (2.0 * len(strong_noise))
                score -= penalty
                reasons.append(f"noise penalty ({-penalty:g})")
                if strong_noise:
                    suppressed = True
                    suppress_reasons.extend(strong_noise)
        if source_type == "agent_session_log" and not agent_query and not suppressed:
            score -= 4.0
            reasons.append("agent log downranked for non-agent query (-4)")

        if not suppressed and apply_recency:
            recency_boost, recency_reason = recency_score(candidate)
            if recency_boost:
                if recency_intent:
                    recency_boost *= 2
                    recency_reason = recency_reason.replace(
                        "recency boost", "recency intent boost"
                    )
                score += recency_boost
                reasons.append(recency_reason)

        lineage = lineage_scores.get(chunk_id) or {}
        lineage_boost = float(lineage.get("boost") or 0.0)
        if lineage_boost:
            score += lineage_boost
            reasons.extend(
                lineage.get("reasons")
                or [f"lineage tie-breaker ({lineage_boost:+.2f})"]
            )

        candidate["retrieval_score"] = round(score, 4)
        candidate["raw_context"] = raw_context_links(candidate)
        candidate["selection_reasons"] = reasons
        candidate["suppressed"] = suppressed
        candidate["suppress_reasons"] = suppress_reasons
        candidate["retrieval_noise_reasons"] = noise_reasons
        candidate["lineage_score"] = round(lineage_boost, 4)
        candidate["lineage_reasons"] = lineage.get("reasons") or []
        candidate["entity_anchor_title_heading_match"] = bool(title_heading_anchor_hits)
        candidate["query_title_hits"] = title_hits
        candidate["query_local_hits"] = local_term_hits
        candidate["query_local_hit_count"] = len(local_term_hits)
        candidate["query_specific_terms"] = specific_terms
        candidate["query_local_specific_hits"] = local_specific_hits
        candidate["query_local_specific_hit_count"] = len(local_specific_hits)
        candidate["required_specific_hit_count"] = required_specific_hits
        scored.append(candidate)

    scored.sort(
        key=lambda row: (
            row.get("suppressed", False),
            -float(row["retrieval_score"]),
            row["chunk_index"],
        )
    )
    return scored


def select_context_chunks(
    reranked_chunks: list[dict[str, Any]], query: str, policy: RetrievalPolicy
) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    remaining = max(policy.total_budget, 1)
    eligible = [
        row
        for row in reranked_chunks
        if chunk_passes_relevance_floor(row, CONTEXT_CHUNK_SCORE_FLOOR)
    ]
    anchored = [row for row in eligible if row.get("entity_anchor_title_heading_match")]
    if anchored:
        eligible = anchored
    non_transcript = [
        row
        for row in eligible
        if "raw transcript chunk" not in row.get("retrieval_noise_reasons", [])
    ]
    if len(non_transcript) >= min(3, policy.max_chunks):
        eligible = non_transcript
    document_counts: dict[str, int] = {}
    for row in eligible:
        if len(selected) >= policy.max_chunks or remaining <= 0:
            break
        document_id = str(row.get("document_id") or "")
        if document_id:
            count = document_counts.get(document_id, 0)
            if count >= MAX_CHUNKS_PER_DOCUMENT:
                continue
        chunk = apply_retrieval_policy(row, query, policy, remaining)
        if not chunk:
            continue
        selected.append(chunk)
        if document_id:
            document_counts[document_id] = document_counts.get(document_id, 0) + 1
        remaining -= int(chunk.get("returned_token_count") or 0)
    return selected


def select_search_results(
    reranked_chunks: list[dict[str, Any]], limit: int
) -> list[dict[str, Any]]:
    if limit <= 0:
        return []
    selected: list[dict[str, Any]] = []
    document_counts: dict[str, int] = {}
    for row in reranked_chunks:
        if not chunk_passes_relevance_floor(row, SEARCH_RESULT_SCORE_FLOOR):
            continue
        document_id = str(row.get("document_id") or "")
        if (
            document_id
            and document_counts.get(document_id, 0) >= MAX_CHUNKS_PER_DOCUMENT
        ):
            continue
        selected.append(row)
        if document_id:
            document_counts[document_id] = document_counts.get(document_id, 0) + 1
        if len(selected) >= limit:
            return selected
    return selected


def chunk_passes_relevance_floor(row: dict[str, Any], floor: float) -> bool:
    if row.get("suppressed"):
        return False
    score = float(row.get("retrieval_score") or 0.0)
    if score < floor:
        return False
    local_hit_count = int(row.get("query_local_hit_count") or 0)
    required_specific_hits = int(row.get("required_specific_hit_count") or 0)
    local_specific_hits = int(row.get("query_local_specific_hit_count") or 0)
    if required_specific_hits and local_specific_hits < required_specific_hits:
        return False
    if local_hit_count > 0:
        return True
    if row.get("entity_anchor_title_heading_match"):
        return True
    return False


def relevant_memories_for_query(
    memories: list[dict[str, Any]],
    query: str,
    limit: int,
    score_floor: int,
) -> list[dict[str, Any]]:
    if limit <= 0:
        return []
    terms = important_query_terms(query)
    specific_terms = specific_query_terms(query)
    if not terms:
        return []
    scored: list[dict[str, Any]] = []
    for memory in memories:
        haystack = " ".join(
            [
                str(memory.get("memory_type") or ""),
                str(memory.get("scope") or ""),
                str(memory.get("content") or ""),
                " ".join(memory.get("source_ids") or []),
            ]
        ).lower()
        matches = terms_in_text(terms, haystack)
        specific_matches = terms_in_text(specific_terms, haystack)
        score = len(matches)
        if score < score_floor:
            continue
        if not specific_matches and score < max(2, score_floor):
            continue
        row = dict(memory)
        row["memory_relevance_score"] = score
        row["matched_query_terms"] = matches
        row["matched_specific_query_terms"] = specific_matches
        scored.append(row)
    scored.sort(
        key=lambda row: (
            -int(row.get("memory_relevance_score") or 0),
            -float(row.get("confidence") or 0.0),
            row.get("id") or "",
        )
    )
    return scored[:limit]


def slim_retrieve_context_packet(
    result: dict[str, Any], policy: RetrievalPolicy
) -> dict[str, Any]:
    packet = dict(result)
    packet["retrieval_reasons"] = list(packet.get("retrieval_reasons") or [])[:5]
    packet["active_memories"] = [
        slim_memory_for_context(row) for row in packet.get("active_memories") or []
    ]
    packet["candidate_memories"] = [
        slim_memory_for_context(row) for row in packet.get("candidate_memories") or []
    ]
    packet["relevant_wiki_pages"] = [
        slim_wiki_page_for_context(row)
        for row in packet.get("relevant_wiki_pages") or []
    ]
    packet["relevant_facts"] = [
        slim_fact_for_context(row) for row in packet.get("relevant_facts") or []
    ]
    packet["supporting_chunks"] = [
        slim_chunk_for_context(row) for row in packet.get("supporting_chunks") or []
    ]
    packet["open_questions"] = [
        slim_open_question_for_context(row)
        for row in packet.get("open_questions") or []
    ]
    packet["citation_snapshots"] = slim_citation_snapshots(
        packet.get("citation_snapshots") or []
    )
    return trim_context_packet_to_serialized_limit(packet, policy)


def slim_memory_for_context(memory: dict[str, Any]) -> dict[str, Any]:
    return keep_present(
        {
            "id": memory.get("id"),
            "memory_type": memory.get("memory_type"),
            "scope": memory.get("scope"),
            "content": truncate_for_packet(str(memory.get("content") or ""), 1200),
            "confidence": memory.get("confidence"),
            "source_ids": list(memory.get("source_ids") or [])[:3],
            "status": memory.get("status"),
            "created_at": memory.get("created_at"),
            "updated_at": memory.get("updated_at"),
            "reviewed_at": memory.get("reviewed_at"),
            "memory_relevance_score": memory.get("memory_relevance_score"),
        }
    )


def slim_wiki_page_for_context(page: dict[str, Any]) -> dict[str, Any]:
    return keep_present(
        {
            "title": page.get("title"),
            "page_type": page.get("page_type"),
            "path": page.get("path"),
            "relative_path": page.get("relative_path"),
            "source_ids": list(page.get("source_ids") or [])[:3],
            "source_count": page.get("source_count"),
            "summary": truncate_for_packet(str(page.get("summary") or ""), 800),
            "score": page.get("score"),
            "managed": page.get("managed"),
        }
    )


def slim_chunk_for_context(chunk: dict[str, Any]) -> dict[str, Any]:
    return keep_present(
        {
            "chunk_id": chunk.get("chunk_id"),
            "document_id": chunk.get("document_id"),
            "origin_node_id": chunk.get("origin_node_id"),
            "logical_source_key": chunk.get("logical_source_key"),
            "source_type": chunk.get("source_type"),
            "title": chunk.get("title"),
            "heading_path": chunk.get("heading_path"),
            "text": chunk.get("text"),
            "retrieval_score": chunk.get("retrieval_score"),
            "score": chunk.get("retrieval_score"),
            "raw_context": chunk.get("raw_context"),
            "original_token_count": chunk.get("original_token_count"),
            "returned_token_count": chunk.get("returned_token_count"),
            "omitted_tokens": chunk.get("omitted_tokens"),
            "excerpted": chunk.get("excerpted"),
            "retrieval_mode": chunk.get("retrieval_mode"),
            "source_token_cap": chunk.get("source_token_cap"),
        }
    )


def slim_open_question_for_context(question: dict[str, Any]) -> dict[str, Any]:
    output = {
        key: question.get(key)
        for key in [
            "id",
            "kind",
            "entity_key",
            "page_hint",
            "fact_ids",
            "question",
            "status",
            "risk_tier",
            "created_at",
            "answered_at",
        ]
    }
    options = question.get("options")
    if isinstance(options, list):
        output["options"] = options[:3]
    return keep_present(output)


def slim_citation_snapshots(
    snapshots: list[dict[str, Any]],
    *,
    limit: int = NON_DEBUG_CITATION_LIMIT,
    text_chars: int = NON_DEBUG_CITATION_TEXT_CHARS,
) -> list[dict[str, Any]]:
    slimmed: list[dict[str, Any]] = []
    for snapshot in snapshots[:limit]:
        if not isinstance(snapshot, dict):
            continue
        entry = dict(snapshot)
        if "text" in entry:
            entry["text"] = truncate_for_packet(
                str(entry.get("text") or ""), text_chars
            )
        if isinstance(entry.get("source_ids"), list):
            entry["source_ids"] = entry["source_ids"][:3]
        slimmed.append(keep_present(entry))
    return slimmed


def stored_citation_snapshots(
    snapshots: list[dict[str, Any]], *, debug: bool
) -> list[dict[str, Any]]:
    if debug:
        return slim_citation_snapshots(
            snapshots,
            limit=DEBUG_CITATION_LIMIT,
            text_chars=DEBUG_CITATION_TEXT_CHARS,
        )
    return slim_citation_snapshots(snapshots)


def stored_retrieval_debug(payload: dict[str, Any]) -> str:
    serialized = dumps(payload)
    byte_count = len(serialized.encode("utf-8"))
    if byte_count <= RETRIEVAL_EVENT_DEBUG_MAX_BYTES:
        return serialized
    candidate_ids = payload.get("candidate_ids")
    fused = payload.get("fused")
    summary = {
        "truncated": True,
        "original_bytes": byte_count,
        "keys": sorted(str(key) for key in payload),
        "assessment": payload.get("assessment"),
        "vector_unavailable_reason": payload.get("vector_unavailable_reason"),
        "candidate_count": len(candidate_ids)
        if isinstance(candidate_ids, list)
        else len(fused)
        if isinstance(fused, list)
        else None,
    }
    return dumps(keep_present(summary))


def trim_context_packet_to_serialized_limit(
    packet: dict[str, Any], policy: RetrievalPolicy
) -> dict[str, Any]:
    limit = CONTEXT_PACKET_MAX_BYTES.get(
        policy.mode, CONTEXT_PACKET_MAX_BYTES["default"]
    )
    if serialized_packet_size(packet) <= limit:
        return packet

    packet["citation_snapshots"] = []
    if serialized_packet_size(packet) <= limit:
        return packet

    for key in [
        "supporting_chunks",
        "relevant_wiki_pages",
        "candidate_memories",
        "active_memories",
        "relevant_facts",
        "open_questions",
    ]:
        while serialized_packet_size(packet) > limit and len(packet.get(key) or []) > 1:
            packet[key].pop()
        if serialized_packet_size(packet) <= limit:
            return packet

    for token_limit in [900, 600, 350, 180]:
        for chunk in packet.get("supporting_chunks") or []:
            text = str(chunk.get("text") or "")
            if estimate_tokens(text) > token_limit:
                chunk["text"] = trim_to_token_budget(text, token_limit)
                chunk["returned_token_count"] = estimate_tokens(chunk["text"])
                original = int(
                    chunk.get("original_token_count") or chunk["returned_token_count"]
                )
                chunk["omitted_tokens"] = max(
                    0, original - int(chunk["returned_token_count"])
                )
                chunk["excerpted"] = chunk["omitted_tokens"] > 0
        if serialized_packet_size(packet) <= limit:
            return packet

    for char_limit in [800, 400, 200]:
        trim_packet_strings(packet, char_limit)
        if serialized_packet_size(packet) <= limit:
            return packet

    return packet


def serialized_packet_size(packet: dict[str, Any]) -> int:
    return len(dumps(packet).encode("utf-8"))


def keep_present(values: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in values.items() if value is not None}


def truncate_for_packet(value: str, max_chars: int) -> str:
    if len(value) <= max_chars:
        return value
    if max_chars <= 16:
        return value[:max_chars]
    suffix = " ... [truncated]"
    return value[: max_chars - len(suffix)].rstrip() + suffix


def trim_packet_strings(value: Any, max_chars: int) -> None:
    if isinstance(value, dict):
        for key, child in list(value.items()):
            if isinstance(child, str) and key in {
                "content",
                "evidence_quote",
                "question",
                "summary",
                "text",
            }:
                value[key] = truncate_for_packet(child, max_chars)
            else:
                trim_packet_strings(child, max_chars)
    elif isinstance(value, list):
        for child in value:
            trim_packet_strings(child, max_chars)


def retrieval_assessment(
    chunks: list[dict[str, Any]],
    wiki_pages: list[dict[str, Any]],
    active_memories: list[dict[str, Any]],
    candidate_memories: list[dict[str, Any]],
    relevant_facts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    relevant_facts = relevant_facts or []
    top_chunk_score = max(
        (float(row.get("retrieval_score") or 0.0) for row in chunks), default=0.0
    )
    top_wiki_score = max(
        (float(row.get("score") or 0.0) for row in wiki_pages), default=0.0
    )
    top_fact_score = max(
        (float(row.get("retrieval_score") or 0.0) for row in relevant_facts),
        default=0.0,
    )
    top_active_memory_score = max(
        (int(row.get("memory_relevance_score") or 0) for row in active_memories),
        default=0,
    )
    top_candidate_memory_score = max(
        (int(row.get("memory_relevance_score") or 0) for row in candidate_memories),
        default=0,
    )
    signal = max(
        top_chunk_score,
        top_wiki_score,
        top_fact_score,
        float(top_active_memory_score * 8),
        float(top_candidate_memory_score * 6),
    )
    reasons = [
        f"top chunk score {top_chunk_score:.1f}",
        f"top wiki score {top_wiki_score:.1f}",
        f"top fact score {top_fact_score:.1f} across {len(relevant_facts)} relevant facts",
        f"top active memory hits {top_active_memory_score}",
        f"top candidate memory hits {top_candidate_memory_score}",
    ]
    if (
        top_chunk_score >= FOUND_CHUNK_SCORE
        or top_wiki_score >= FOUND_WIKI_SCORE
        or top_fact_score >= FOUND_FACT_SCORE
        or top_active_memory_score >= 2
    ):
        verdict = "found"
    elif (
        top_chunk_score >= CONTEXT_CHUNK_SCORE_FLOOR
        or top_wiki_score >= WIKI_PAGE_SCORE_FLOOR
        or top_fact_score >= FACT_SCORE_FLOOR
        or top_active_memory_score >= MEMORY_ACTIVE_SCORE_FLOOR
        or top_candidate_memory_score >= MEMORY_CANDIDATE_SCORE_FLOOR
    ):
        verdict = "partial"
    else:
        verdict = "no_strong_match"
        reasons.append("no selected evidence passed the relevance floor")
    confidence = round(min(1.0, signal / 40.0), 3)
    return {
        "verdict": verdict,
        "confidence": confidence,
        "reasons": reasons,
    }


def apply_retrieval_policy(
    chunk: dict[str, Any],
    query: str,
    policy: RetrievalPolicy,
    remaining_budget: int,
) -> dict[str, Any] | None:
    if remaining_budget <= 0:
        return None
    source_type = str(chunk.get("source_type") or "")
    source_cap = policy.source_caps.get(source_type, policy.default_chunk_cap)
    allowed = min(remaining_budget, source_cap)
    if allowed <= 0:
        return None

    original_text = str(chunk.get("text") or "")
    original_count = int(chunk.get("token_count") or estimate_tokens(original_text))
    if policy.include_full_text and original_count <= allowed:
        text = original_text
    elif original_count <= allowed:
        text = original_text
    else:
        text = excerpt_chunk_text(original_text, query, source_type, allowed)

    returned_count = estimate_tokens(text)
    if returned_count > allowed:
        text = trim_to_token_budget(text, allowed)
        returned_count = estimate_tokens(text)

    output = dict(chunk)
    output["text"] = text
    output["original_token_count"] = original_count
    output["returned_token_count"] = returned_count
    output["omitted_tokens"] = max(0, original_count - returned_count)
    output["excerpted"] = returned_count < original_count
    output["retrieval_mode"] = policy.mode
    output["source_token_cap"] = source_cap
    return output


def excerpt_chunk_text(text: str, query: str, source_type: str, max_tokens: int) -> str:
    cleaned = clean_context_text(text, source_type)
    if estimate_tokens(cleaned) <= max_tokens:
        return cleaned
    excerpt = focused_excerpt(cleaned, query, max_tokens)
    if excerpt:
        return excerpt
    return trim_to_token_budget(cleaned, max_tokens)


def clean_context_text(text: str, source_type: str) -> str:
    cleaned = strip_frontmatter(text)
    if source_type == "agent_session_log":
        return clean_agent_session_log(cleaned)
    return cleaned.strip()


def strip_frontmatter(text: str) -> str:
    stripped = text.lstrip()
    if not stripped.startswith("---"):
        return text.strip()
    end = stripped.find("\n---", 3)
    if end == -1:
        return text.strip()
    return stripped[end + 4 :].strip()


def clean_agent_session_log(text: str) -> str:
    return sanitize_agent_session_log(text)


def focused_excerpt(text: str, query: str, max_tokens: int) -> str:
    terms = set(important_query_terms(query))
    blocks = context_blocks(text)
    scored: list[tuple[int, int, str]] = []
    for index, block in enumerate(blocks):
        lowered = block.lower()
        hits = sum(1 for term in terms if contains_query_term(lowered, term))
        section_bonus = 0
        if any(
            marker in lowered
            for marker in ["## user requests", "## summary", "## assistant responses"]
        ):
            section_bonus = 2
        if hits or section_bonus:
            scored.append((hits + section_bonus, index, block))
    if not scored:
        return trim_to_token_budget(text, max_tokens)

    selected_indexes = {
        index
        for _, index, _ in sorted(scored, key=lambda item: (-item[0], item[1]))[:8]
    }
    selected_blocks = [
        block for index, block in enumerate(blocks) if index in selected_indexes
    ]
    return pack_blocks(selected_blocks, max_tokens, terms)


def context_blocks(text: str) -> list[str]:
    raw_blocks = re.split(r"\n\s*\n", text)
    blocks: list[str] = []
    for block in raw_blocks:
        stripped = block.strip()
        if not stripped:
            continue
        if estimate_tokens(stripped) > 220:
            lines = [line.strip() for line in stripped.splitlines() if line.strip()]
            blocks.extend(line for line in lines if line)
        else:
            blocks.append(stripped)
    return blocks


def pack_blocks(blocks: list[str], max_tokens: int, terms: set[str]) -> str:
    selected: list[str] = []
    remaining = max_tokens
    for block in blocks:
        if remaining <= 0:
            break
        block_tokens = estimate_tokens(block)
        if block_tokens > remaining:
            selected.append(trim_to_token_budget_around_terms(block, remaining, terms))
            remaining = 0
        else:
            selected.append(block)
            remaining -= block_tokens
    return "\n\n".join(selected).strip()


def trim_to_token_budget(text: str, max_tokens: int) -> str:
    words = re.findall(r"\S+", text)
    if len(words) <= max_tokens:
        return text.strip()
    if max_tokens <= 3:
        return " ".join(words[:max_tokens])
    return " ".join(words[: max_tokens - 3] + ["...", "[truncated]"])


def trim_to_token_budget_around_terms(
    text: str, max_tokens: int, terms: set[str]
) -> str:
    words = re.findall(r"\S+", text)
    if len(words) <= max_tokens:
        return text.strip()
    if max_tokens <= 6 or not terms:
        return trim_to_token_budget(text, max_tokens)
    lowered_terms = {term.lower() for term in terms}
    hit_index = next(
        (
            index
            for index, word in enumerate(words)
            if any(term in word.lower() for term in lowered_terms)
        ),
        0,
    )
    body_budget = max_tokens - 4
    start = max(0, hit_index - max(0, body_budget // 3))
    end = min(len(words), start + body_budget)
    if end - start < body_budget:
        start = max(0, end - body_budget)
    excerpt_words = words[start:end]
    prefix = ["...", "[excerpt]"] if start > 0 else []
    suffix = ["...", "[truncated]"] if end < len(words) else []
    return " ".join(prefix + excerpt_words + suffix)


def chunk_noise_reasons(chunk: dict[str, Any]) -> list[str]:
    text = str(chunk.get("text") or "")
    text_lower = text.lower()
    heading = str(chunk.get("heading_path") or "").lower()
    source_type = str(chunk.get("source_type") or "")
    reasons: list[str] = []
    if source_type == "agent_session_log":
        if "session_meta:" in text_lower or "- session_meta:" in text_lower:
            reasons.append("session metadata")
        if "you are codex" in text_lower or "<permissions instructions>" in text_lower:
            reasons.append("system prompt text")
        trace_markers = text_lower.count("event_msg") + text_lower.count(
            "response_item"
        )
        if trace_markers >= 2:
            reasons.append("tool/session trace")
    if looks_frontmatter_only(text):
        reasons.append("frontmatter-only chunk")
    if "no summary was captured" in text_lower and len(text_lower) < 600:
        reasons.append("empty generated summary")
    if "transcript" in heading or text_lower.startswith("## transcript"):
        reasons.append("raw transcript chunk")
    return dedupe_preserve_order(reasons)


def retrieval_negative_control_fixture_mention(
    text_lower: str, terms: list[str], local_specific_hits: list[str]
) -> bool:
    if not terms:
        return False
    if set(terms) & NEGATIVE_CONTROL_QUERY_TERMS:
        return False
    if not any(marker in text_lower for marker in NEGATIVE_CONTROL_CONTEXT_MARKERS):
        return False
    required_hits = max(3, len(terms) if len(terms) <= 4 else len(terms) - 1)
    return len(set(local_specific_hits)) >= required_hits


def recency_score(chunk: dict[str, Any]) -> tuple[float, str]:
    timestamp = parse_iso_timestamp(
        str(chunk.get("document_created_at") or chunk.get("document_ingested_at") or "")
    )
    if not timestamp:
        return 0.0, ""
    age_days = max(
        0.0, (datetime.now(timezone.utc) - timestamp).total_seconds() / 86400.0
    )
    boost = RECENCY_MAX_BOOST * (
        RECENCY_HALF_LIFE_DAYS / (RECENCY_HALF_LIFE_DAYS + age_days)
    )
    if boost < 0.05:
        return 0.0, ""
    return round(boost, 4), f"recency boost age {age_days:.1f}d (+{boost:.2f})"


def parse_iso_timestamp(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def raw_context_links(chunk: dict[str, Any]) -> dict[str, Any]:
    return {
        "document_id": chunk.get("document_id"),
        "chunk_id": chunk.get("chunk_id"),
        "source_path": chunk.get("source_path"),
        "raw_path": chunk.get("raw_path"),
        "source_type": chunk.get("source_type"),
        "title": chunk.get("title"),
        "heading_path": chunk.get("heading_path"),
        "chunk_index": chunk.get("chunk_index"),
        "document_created_at": chunk.get("document_created_at"),
        "document_ingested_at": chunk.get("document_ingested_at"),
    }


def chunk_citation_snapshots(chunks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    snapshots: list[dict[str, Any]] = []
    for chunk in chunks:
        snapshots.append(
            {
                "type": "chunk",
                "chunk_id": str(chunk.get("chunk_id") or ""),
                "document_id": str(chunk.get("document_id") or ""),
                "origin_node_id": chunk.get("origin_node_id"),
                "logical_source_key": str(
                    chunk.get("logical_source_key") or chunk.get("source_path") or ""
                ),
                "content_hash": str(chunk.get("content_hash") or ""),
                "heading_path": chunk.get("heading_path") or "",
                "text": str(chunk.get("text") or ""),
            }
        )
    return snapshots


def wiki_page_citation_snapshots(
    wiki_pages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    snapshots: list[dict[str, Any]] = []
    for page in wiki_pages:
        snapshots.append(
            {
                "type": "wiki_page",
                "relative_path": str(page.get("relative_path") or ""),
                "title": str(page.get("title") or ""),
                "source_ids": list(page.get("source_ids") or []),
            }
        )
    return snapshots


def looks_frontmatter_only(text: str) -> bool:
    stripped = text.strip()
    if not stripped.startswith("---"):
        return False
    end = stripped.find("\n---", 3)
    if end == -1:
        return False
    tail = stripped[end + 4 :].strip()
    return not tail


def build_retrieval_debug(
    query: str,
    fanout_debug: dict[str, Any],
    selected_chunks: list[dict[str, Any]],
    reranked_chunks: list[dict[str, Any]],
    wiki_pages: list[dict[str, Any]],
    debug: bool,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "important_query_terms": important_query_terms(query),
        "selected_chunk_reasons": summarize_ranked_chunks(selected_chunks, limit=8),
        "suppressed_chunk_reasons": summarize_ranked_chunks(
            [row for row in reranked_chunks if row.get("suppressed")],
            limit=8,
        ),
        "selected_wiki_reasons": [
            {
                "relative_path": page.get("relative_path"),
                "score": page.get("score"),
                "reasons": page.get("selection_reasons", []),
            }
            for page in wiki_pages
        ],
    }
    if debug:
        payload["fanout"] = fanout_debug
        payload["reranked_candidates"] = summarize_ranked_chunks(
            reranked_chunks, limit=30
        )
    else:
        payload["fanout_counts"] = {
            "lexical": len(fanout_debug.get("lexical", [])),
            "vector": len(fanout_debug.get("vector", [])),
            "fused": len(fanout_debug.get("fused", [])),
            "candidates": len(fanout_debug.get("candidate_ids", [])),
        }
    return payload


def build_search_debug(
    fanout_debug: dict[str, Any],
    selected_chunks: list[dict[str, Any]],
    reranked_chunks: list[dict[str, Any]],
    debug: bool,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "fanout_counts": {
            "lexical": len(fanout_debug.get("lexical", [])),
            "vector": len(fanout_debug.get("vector", [])),
            "fused": len(fanout_debug.get("fused", [])),
            "candidates": len(fanout_debug.get("candidate_ids", [])),
        },
        "selected_chunk_reasons": summarize_ranked_chunks(
            selected_chunks, limit=len(selected_chunks)
        ),
        "suppressed_chunk_reasons": summarize_ranked_chunks(
            [row for row in reranked_chunks if row.get("suppressed")],
            limit=8,
        ),
    }
    if debug:
        payload["fanout"] = fanout_debug
        payload["reranked_candidates"] = summarize_ranked_chunks(
            reranked_chunks, limit=30
        )
    return payload


def summarize_ranked_chunks(
    chunks: list[dict[str, Any]], limit: int
) -> list[dict[str, Any]]:
    return [
        {
            "chunk_id": row.get("chunk_id"),
            "title": row.get("title"),
            "source_type": row.get("source_type"),
            "score": row.get("retrieval_score"),
            "raw_context": row.get("raw_context"),
            "suppressed": row.get("suppressed", False),
            "reasons": row.get("selection_reasons", []),
            "suppress_reasons": row.get("suppress_reasons", []),
            "retrieval_noise_reasons": row.get("retrieval_noise_reasons", []),
            "lineage_score": row.get("lineage_score", 0.0),
            "lineage_reasons": row.get("lineage_reasons", []),
            "query_local_hits": row.get("query_local_hits", []),
            "query_local_specific_hits": row.get("query_local_specific_hits", []),
            "preview": str(row.get("text", ""))[:160],
        }
        for row in chunks[:limit]
    ]


def normalize_lineage_target(target_type: str, target_id: str) -> tuple[str, str]:
    normalized_type = target_type.strip().lower()
    normalized_id = target_id.strip()
    if normalized_type not in {"memory", "chunk", "document", "wiki_page", "fact"}:
        raise ValueError(
            "target_type must be one of memory, chunk, document, wiki_page, or fact"
        )
    if normalized_type == "document" and normalized_id.startswith("document:"):
        normalized_id = normalized_id.split(":", 1)[1]
    if not normalized_id:
        raise ValueError("target_id must not be empty")
    return normalized_type, normalized_id


def insert_chunk_retrieval_fts(
    conn: Any,
    *,
    chunk_id: str,
    title: Any,
    text: Any,
    heading_path: Any,
    project: Any,
    tags: Any,
) -> None:
    serialized_tags = tags if isinstance(tags, str) else dumps(tags or [])
    conn.execute(
        """
        INSERT INTO chunk_fts(chunk_id, title, text, heading_path, project, tags)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (chunk_id, title, text, heading_path, project, serialized_tags),
    )
    if sqlite_table_exists(conn, "retrieval_fts"):
        conn.execute(
            """
            INSERT INTO retrieval_fts(kind, target_id, title, text, heading_path, project, tags)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            ("chunk", chunk_id, title, text, heading_path, project, serialized_tags),
        )


def delete_chunk_retrieval_fts(conn: Any, chunk_ids: list[str]) -> None:
    if not chunk_ids:
        return
    placeholders = ",".join("?" for _ in chunk_ids)
    conn.execute(f"DELETE FROM chunk_fts WHERE chunk_id IN ({placeholders})", chunk_ids)
    if sqlite_table_exists(conn, "retrieval_fts"):
        conn.execute(
            f"DELETE FROM retrieval_fts WHERE kind = 'chunk' AND target_id IN ({placeholders})",
            chunk_ids,
        )


def delete_all_chunk_fts(conn: Any) -> None:
    conn.execute("DELETE FROM chunk_fts")
    if sqlite_table_exists(conn, "retrieval_fts"):
        conn.execute("DELETE FROM retrieval_fts WHERE kind = 'chunk'")


def update_chunk_retrieval_title(conn: Any, document_id: str, title: str) -> None:
    conn.execute(
        """
        UPDATE chunk_fts
        SET title = ?
        WHERE chunk_id IN (SELECT id FROM chunks WHERE document_id = ?)
        """,
        (title, document_id),
    )
    if sqlite_table_exists(conn, "retrieval_fts"):
        conn.execute(
            """
            UPDATE retrieval_fts
            SET title = ?
            WHERE kind = 'chunk'
              AND target_id IN (SELECT id FROM chunks WHERE document_id = ?)
            """,
            (title, document_id),
        )


def sqlite_table_exists(conn: Any, table: str) -> bool:
    return bool(
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type IN ('table', 'virtual') AND name = ?",
            (table,),
        ).fetchone()
    )


def retention_cutoff_iso(older_than_days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=older_than_days)).isoformat()


def sqlite_storage_report(sqlite_path: Path) -> dict[str, Any]:
    related_paths = [
        sqlite_path,
        sqlite_path.with_name(f"{sqlite_path.name}-wal"),
        sqlite_path.with_name(f"{sqlite_path.name}-shm"),
    ]
    report: dict[str, Any] = {
        "sqlite_path": str(sqlite_path),
        "sqlite_related_bytes": sum(path_size(path) for path in related_paths),
        "files": {
            path.name: path_size(path) for path in related_paths if path.exists()
        },
        "page_size": None,
        "page_count": None,
        "freelist_count": None,
        "freelist_bytes": None,
    }
    if not sqlite_path.exists():
        return report
    try:
        with connection(sqlite_path) as conn:
            page_size = int(conn.execute("PRAGMA page_size").fetchone()[0])
            page_count = int(conn.execute("PRAGMA page_count").fetchone()[0])
            freelist_count = int(conn.execute("PRAGMA freelist_count").fetchone()[0])
        report.update(
            {
                "page_size": page_size,
                "page_count": page_count,
                "freelist_count": freelist_count,
                "freelist_bytes": page_size * freelist_count,
            }
        )
    except Exception as exc:
        report["error"] = str(exc)
    return report


def row_to_retrieval_fact(
    row: Any, score: float, *, fts_score: float | None = None
) -> dict[str, Any]:
    from .wiki_facts import row_to_fact

    fact = row_to_fact(row)
    fact["type"] = "fact"
    fact["retrieval_score"] = round(score, 4)
    if fts_score is not None:
        fact["fts_score"] = round(fts_score, 4)
    fact["authoritative"] = fact.get("status") == "active"
    fact["contested"] = fact.get("status") == "conflicted"
    return fact


def score_retrieval_fact_for_query(
    fact: dict[str, Any],
    query: str,
    fts_score: float,
) -> dict[str, Any] | None:
    terms = important_query_terms(query)
    if not terms:
        return None
    specific_terms = specific_query_terms(query)
    required_specific_hits = required_specific_hit_count(specific_terms)
    haystack = " ".join(
        [
            str(fact.get("statement") or ""),
            str(fact.get("page_hint") or ""),
            str(fact.get("section_hint") or ""),
            str(fact.get("entity_key") or ""),
            str(fact.get("evidence_quote") or ""),
            str(fact.get("temporal_expression") or ""),
            str(fact.get("event_time_expression") or ""),
            str(fact.get("event_start_at") or ""),
            str(fact.get("event_end_at") or ""),
            " ".join(str(source_id) for source_id in fact.get("source_ids") or []),
        ]
    )
    matches = terms_in_text(terms, haystack)
    specific_matches = terms_in_text(specific_terms, haystack)
    if not matches:
        return None
    if required_specific_hits and len(specific_matches) < required_specific_hits:
        return None
    if specific_terms and not specific_matches and len(matches) < 2:
        return None
    if legacy_wiki_backfill_fact_without_spans(fact) and len(
        matches
    ) < legacy_wiki_backfill_required_match_count(terms):
        return None

    confidence = float(fact.get("truth_confidence") or fact.get("confidence") or 0.0)
    fts_component = min(FACT_FTS_SCORE_CAP, max(0.0, -float(fts_score or 0.0)))
    score = (
        fts_component
        + (4.0 * len(matches))
        + (3.0 * len(specific_matches))
        + (2.0 * max(0.0, confidence - FACT_TRUTH_CONFIDENCE_FLOOR))
    )
    if score < FACT_SCORE_FLOOR:
        return None

    output = dict(fact)
    output["retrieval_score"] = round(score, 4)
    output["fts_score"] = round(float(fts_score or 0.0), 4)
    output["matched_query_terms"] = matches
    output["matched_specific_query_terms"] = specific_matches
    output["required_specific_hit_count"] = required_specific_hits
    output["fact_relevance_reasons"] = [
        f"FTS component {fts_component:.2f}",
        f"matched {len(matches)}/{len(terms)} important terms",
    ]
    if specific_terms:
        output["fact_relevance_reasons"].append(
            f"matched {len(specific_matches)}/{len(specific_terms)} specific terms"
        )
    return output


def legacy_wiki_backfill_fact_without_spans(fact: dict[str, Any]) -> bool:
    metadata = fact.get("metadata") if isinstance(fact.get("metadata"), dict) else {}
    is_backfill = (
        metadata.get("migration") == "wiki_fact_backfill_v1"
        or metadata.get("source") == "existing_wiki"
    )
    return bool(is_backfill and not fact.get("source_spans"))


def legacy_wiki_backfill_required_match_count(terms: list[str]) -> int:
    if len(terms) >= 3:
        return max(3, (len(terms) + 1) // 2)
    return len(terms)


def dynamic_fact_cut(facts: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    previous_score: float | None = None
    for fact in facts:
        score = float(fact.get("retrieval_score") or 0.0)
        if (
            previous_score is not None
            and len(selected) >= FACT_KNEE_MIN_RANK
            and previous_score - score >= FACT_KNEE_DROP
        ):
            break
        selected.append(fact)
        previous_score = score
        if len(selected) >= limit:
            break
    return selected


def suppress_chunks_covered_by_facts(
    chunks: list[dict[str, Any]], facts: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    covered_chunk_ids = {
        str(span.get("chunk_id") or "")
        for fact in facts
        for span in fact.get("source_spans") or []
        if isinstance(span, dict) and span.get("chunk_id")
    }
    if not covered_chunk_ids:
        return chunks
    return [
        chunk
        for chunk in chunks
        if str(chunk.get("chunk_id") or "") not in covered_chunk_ids
    ]


def open_question_row_to_context(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "kind": row["kind"],
        "entity_key": row["entity_key"],
        "page_hint": row["page_hint"],
        "fact_ids": loads(row["fact_ids"], []),
        "question": row["question"],
        "options": loads(row["options"], []),
        "status": row["status"],
        "answer": loads(row["answer"], None),
        "context": loads(row["context"], {}),
        "action_id": row_value(row, "action_id"),
        "recommended_action": loads(row_value(row, "recommended_action"), {}),
        "auto_resolve_after": row_value(row, "auto_resolve_after"),
        "risk_tier": row_value(row, "risk_tier"),
        "resolver": row_value(row, "resolver"),
        "decided_by": row_value(row, "decided_by"),
        "created_at": row["created_at"],
        "answered_at": row["answered_at"],
    }


def row_value(row: Any, key: str, default: Any = None) -> Any:
    try:
        if hasattr(row, "keys") and key not in row.keys():
            return default
        value = row[key]
    except (KeyError, IndexError, TypeError):
        return default
    return default if value is None else value


def _descending_text_sort_key(value: str) -> tuple[int, ...]:
    return tuple(-ord(char) for char in value)


def insert_context_lineage_event(
    conn: Any,
    event_id: str,
    target_type: str,
    target_id: str,
    event_type: str,
    retrieval_event_id: str | None,
    agent_session_id: str | None,
    query: str | None,
    weight: float,
    metadata: dict[str, Any] | None,
    created_at: str,
) -> None:
    conn.execute(
        """
        INSERT INTO context_lineage_events(
          id, target_type, target_id, event_type, retrieval_event_id,
          agent_session_id, query, weight, metadata, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event_id,
            target_type,
            target_id,
            event_type,
            retrieval_event_id,
            agent_session_id,
            query,
            float(weight),
            dumps(metadata or {}),
            created_at,
        ),
    )


def lineage_decay(created_at: str, now: datetime) -> float:
    timestamp = parse_iso_timestamp(created_at)
    if timestamp is None:
        return 1.0
    age_days = max(0.0, (now - timestamp).total_seconds() / 86400.0)
    return 0.5 ** (age_days / LINEAGE_HALF_LIFE_DAYS)


def lineage_reason_strings(
    reason_values: dict[str, float | int], capped_boost: float
) -> list[str]:
    reasons: list[str] = []
    for event_type in ["explicit_useful", "explicit_not_useful", "agent_referenced_id"]:
        value = float(reason_values.get(event_type, 0.0))
        count = int(reason_values.get(f"{event_type}:count", 0))
        if not value:
            continue
        label = event_type.replace("_", " ")
        reasons.append(f"lineage {label} x{count} ({value:+.2f})")
    total = sum(
        float(reason_values.get(event_type, 0.0))
        for event_type in [
            "explicit_useful",
            "explicit_not_useful",
            "agent_referenced_id",
        ]
    )
    if abs(total - capped_boost) > 0.01:
        reasons.append(f"lineage capped tie-breaker ({capped_boost:+.2f})")
    return reasons or [f"lineage tie-breaker ({capped_boost:+.2f})"]


@dataclass
class ReplacedDocuments:
    documents: int
    chunk_ids: list[str]


def deterministic_mirror_id(prefix: str, value: str) -> str:
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]
    return f"{prefix}_mirror_{digest}"


def document_text_path(document: dict[str, Any]) -> Path | None:
    for key in ("raw_path", "source_path"):
        path = Path(str(document.get(key) or ""))
        if path.exists() and path.is_file():
            return path
    return None


def remove_mirror_index_documents(conn: Any, raw_root: Path) -> list[str]:
    raw_root = raw_root.resolve()
    stale_documents = []
    for row in conn.execute(
        "SELECT id, source_path FROM documents WHERE origin_node_id = 'mirror'"
    ):
        try:
            Path(str(row["source_path"])).resolve().relative_to(raw_root)
        except ValueError:
            continue
        stale_documents.append(dict(row))
    if not stale_documents:
        return []
    document_ids = [row["id"] for row in stale_documents]
    placeholders = ",".join("?" for _ in document_ids)
    stale_chunk_ids = [
        row["id"]
        for row in conn.execute(
            f"SELECT id FROM chunks WHERE document_id IN ({placeholders})",
            document_ids,
        )
    ]
    if stale_chunk_ids:
        delete_chunk_retrieval_fts(conn, stale_chunk_ids)
    conn.execute(f"DELETE FROM documents WHERE id IN ({placeholders})", document_ids)
    return stale_chunk_ids


def remove_documents(
    conn: Any, document_rows: list[dict[str, Any]]
) -> ReplacedDocuments:
    if not document_rows:
        return ReplacedDocuments(0, [])
    document_ids = [row["id"] for row in document_rows]
    placeholders = ",".join("?" for _ in document_ids)
    stale_chunk_ids = [
        row["id"]
        for row in conn.execute(
            f"SELECT id FROM chunks WHERE document_id IN ({placeholders})",
            document_ids,
        )
    ]
    if stale_chunk_ids:
        delete_chunk_retrieval_fts(conn, stale_chunk_ids)
    conn.execute(f"DELETE FROM documents WHERE id IN ({placeholders})", document_ids)

    for row in document_rows:
        raw_path = Path(str(row["raw_path"]))
        try:
            if raw_path.exists():
                raw_path.unlink()
        except OSError:
            pass
    return ReplacedDocuments(len(document_rows), stale_chunk_ids)


def remove_superseded_agent_session_snapshots(
    conn: Any,
    path: Path,
    origin_node_id: str | None = None,
    keep_document_id: str | None = None,
) -> ReplacedDocuments:
    query = """
        SELECT id, raw_path
        FROM documents
        WHERE source_type = 'agent_session_log'
          AND source_path = ?
    """
    params: list[Any] = [str(path)]
    if origin_node_id is not None:
        query += " AND origin_node_id = ?"
        params.append(origin_node_id)
    if keep_document_id:
        query += " AND id != ?"
        params.append(keep_document_id)
    stale_documents = [dict(row) for row in conn.execute(query, params)]
    return remove_documents(conn, stale_documents)


def detect_source_type(path: Path) -> str | None:
    suffix = path.suffix.lower()
    if suffix == ".md":
        text = path.read_text(encoding="utf-8", errors="replace")[:2000].lower()
        if re.search(
            r"source_type:\s*['\"]?gmail_thread", text
        ) or "/documents/gmail/" in str(path):
            return "gmail_thread"
        if re.search(
            r"source_type:\s*['\"]?hyprnote_meeting", text
        ) or "/documents/hyprnote/" in str(path):
            return "hyprnote_meeting"
        if re.search(
            r"source_type:\s*['\"]?agent_session_log", text
        ) or "/agent_logs/" in str(path):
            return "agent_session_log"
        return (
            "agent_session_log"
            if "commands" in text and "outcome" in text
            else "markdown_note"
        )
    if suffix == ".txt":
        return "meeting_transcript"
    if suffix in {".json", ".jsonl"}:
        return "agent_session_log"
    return None


def markdown_frontmatter_value(text: str, key: str) -> str | None:
    if not text.startswith("---\n"):
        return None
    end = text.find("\n---", 4)
    if end == -1:
        return None
    prefix = f"{key}:"
    for line in text[4:end].splitlines():
        if line.startswith(prefix):
            value = line[len(prefix) :].strip()
            return value.strip('"').strip("'").strip() or None
    return None


def document_title_for_text(text: str, path: Path) -> str:
    fallback = path.stem.replace("-", " ").replace("_", " ").strip() or path.name
    return bounded_document_title(markdown_frontmatter_value(text, "title"), fallback)


def stable_lineage_references(text: str) -> list[tuple[str, str, str]]:
    references: list[tuple[str, str, str]] = []
    for memory_id in re.findall(r"\bmem_[A-Za-z0-9_]+\b", text):
        references.append(("memory", memory_id, memory_id))
    for chunk_id in re.findall(r"\bchunk_[A-Za-z0-9_]+\b", text):
        references.append(("chunk", chunk_id, chunk_id))
    for document_source_id in re.findall(r"\bdocument:doc_[A-Za-z0-9_]+\b", text):
        references.append(
            ("document", document_source_id.split(":", 1)[1], document_source_id)
        )
    for document_id in re.findall(r"\bdoc_[A-Za-z0-9_]+\b", text):
        references.append(("document", document_id, document_id))
    wiki_pattern = re.compile(
        r"\b((?:concepts|decisions|projects|people|open_loops|timelines|references)/(?:[A-Za-z0-9._-]+/)*[A-Za-z0-9._-]+\.md)\b"
    )
    for wiki_path in wiki_pattern.findall(text):
        references.append(("wiki_page", wiki_path, wiki_path))

    deduped: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str]] = set()
    for target_type, target_id, original in references:
        key = (target_type, target_id)
        if key in seen:
            continue
        seen.add(key)
        deduped.append((target_type, target_id, original))
    return deduped


def record_agent_log_lineage_references(
    conn: Any,
    text: str,
    document_id: str,
    agent_session_id: str | None,
    created_at: str,
) -> int:
    references = stable_lineage_references(text)
    if not references:
        return 0
    session_id = agent_session_id or f"document:{document_id}"
    created = 0
    for target_type, target_id, original in references:
        exists = conn.execute(
            """
            SELECT 1
            FROM context_lineage_events
            WHERE target_type = ?
              AND target_id = ?
              AND event_type = 'agent_referenced_id'
              AND agent_session_id = ?
            LIMIT 1
            """,
            (target_type, target_id, session_id),
        ).fetchone()
        if exists:
            continue
        insert_context_lineage_event(
            conn,
            event_id=new_id("lineage"),
            target_type=target_type,
            target_id=target_id,
            event_type="agent_referenced_id",
            retrieval_event_id=None,
            agent_session_id=session_id,
            query=None,
            weight=LINEAGE_EVENT_WEIGHTS["agent_referenced_id"],
            metadata={
                "document_id": document_id,
                "source_id": f"document:{document_id}",
                "referenced_id": original,
            },
            created_at=created_at,
        )
        created += 1
    return created


def refresh_existing_document_metadata(
    conn: Any,
    document_id: str,
    source_type: str,
    title: str,
    path: Path,
    origin_node_id: str,
    logical_source_key: str,
    *,
    source_mtime_ns: int | None = None,
    source_size: int | None = None,
) -> None:
    conn.execute(
        """
        UPDATE documents
        SET source_type = ?, title = ?, source_path = ?, origin_node_id = ?, logical_source_key = ?,
            source_mtime_ns = ?, source_size = ?
        WHERE id = ?
        """,
        (
            source_type,
            title,
            str(path),
            origin_node_id,
            logical_source_key,
            source_mtime_ns,
            source_size,
            document_id,
        ),
    )
    update_chunk_retrieval_title(conn, document_id, title)


def existing_document_matches_source_stats(
    document: Any,
    *,
    source_mtime_ns: int,
    source_size: int,
) -> bool:
    return (
        document["source_mtime_ns"] is not None
        and document["source_size"] is not None
        and int(document["source_mtime_ns"]) == source_mtime_ns
        and int(document["source_size"]) == source_size
    )


def reciprocal_rank_fusion(*rankings: list[str], k: int = 60) -> list[str]:
    scores: dict[str, float] = {}
    for ranking in rankings:
        for rank, chunk_id in enumerate(ranking, start=1):
            scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (k + rank)
    return [
        chunk_id
        for chunk_id, _ in sorted(
            scores.items(), key=lambda item: item[1], reverse=True
        )
    ]


def build_fts_query(query: str) -> str:
    terms = important_query_terms(query)
    return " OR ".join(f'"{term}"' for term in terms)


def query_terms(query: str) -> list[str]:
    stopwords = {
        "a",
        "an",
        "and",
        "any",
        "are",
        "as",
        "at",
        "be",
        "been",
        "being",
        "by",
        "can",
        "could",
        "did",
        "do",
        "does",
        "for",
        "how",
        "i",
        "if",
        "in",
        "into",
        "is",
        "it",
        "me",
        "might",
        "must",
        "my",
        "next",
        "no",
        "not",
        "of",
        "on",
        "or",
        "s",
        "shall",
        "should",
        "some",
        "that",
        "the",
        "these",
        "this",
        "those",
        "to",
        "what",
        "whether",
        "will",
        "with",
        "would",
        "yes",
        "you",
        "your",
    }
    output: list[str] = []
    seen: set[str] = set()
    for raw in re.findall(r"[A-Za-z0-9_]+", query):
        term = raw.lower()
        if term in stopwords:
            continue
        if len(term) < 2:
            continue
        if term in seen:
            continue
        seen.add(term)
        output.append(term)
    return output


def important_query_terms(query: str) -> list[str]:
    terms = [term for term in query_terms(query) if term not in GENERIC_CONTEXT_TERMS]
    return terms or query_terms(query)


def specific_query_terms(query: str) -> list[str]:
    return [
        term
        for term in important_query_terms(query)
        if term not in BROAD_RETRIEVAL_TERMS
    ]


def required_specific_hit_count(specific_terms: list[str]) -> int:
    if len(specific_terms) >= 4:
        return 2
    if specific_terms:
        return 1
    return 0


def contains_query_term(haystack: str, term: str) -> bool:
    if not haystack or not term:
        return False
    if len(term) <= 3:
        return (
            re.search(rf"(?<![A-Za-z0-9_]){re.escape(term)}(?![A-Za-z0-9_])", haystack)
            is not None
        )
    return term in haystack


def terms_in_text(terms: list[str], haystack: str) -> list[str]:
    lowered = haystack.lower()
    return sorted({term for term in terms if contains_query_term(lowered, term)})


def anchor_query_terms(
    terms: list[str], chunks: list[dict[str, Any]], agent_query: bool = False
) -> list[str]:
    anchors: list[str] = []
    for term in terms:
        if term in GENERIC_BUSINESS_TERMS or len(term) < 4:
            continue
        for row in chunks:
            source_type = str(row.get("source_type") or "")
            if source_type == "agent_session_log" and not agent_query:
                continue
            title = str(row.get("title") or "").lower()
            heading = str(row.get("heading_path") or "").lower()
            text = str(row.get("text") or "").lower()
            if not contains_query_term(title, term):
                continue
            if source_type == "agent_session_log" and not (
                contains_query_term(heading, term) or contains_query_term(text, term)
            ):
                continue
            anchors.append(term)
            break
    return anchors


def is_agent_query(terms: list[str]) -> bool:
    return bool(set(terms).intersection(AGENT_QUERY_TERMS))


def has_recency_intent(query: str, terms: list[str]) -> bool:
    lowered = query.lower()
    raw_terms = {term.lower() for term in re.findall(r"[A-Za-z0-9_]+", query)}
    return bool(
        set(terms).intersection(RECENCY_INTENT_TERMS)
        or raw_terms.intersection(RECENCY_INTENT_TERMS)
    ) or any(phrase in lowered for phrase in ["this week", "last week", "last month"])


def first_section(markdown: str, heading: str) -> str:
    match = re.search(
        rf"^##\s+{re.escape(heading)}\s*\n+(.*?)(?=^##\s+|\Z)",
        markdown,
        flags=re.MULTILINE | re.DOTALL,
    )
    if not match:
        return ""
    return re.sub(r"\s+", " ", match.group(1)).strip()


def dedupe_preserve_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            output.append(value)
    return output


def unique_backup_path(parent: Path, stem: str) -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    candidate = parent / f"{stem}-{timestamp}"
    suffix = 1
    while candidate.exists():
        suffix += 1
        candidate = parent / f"{stem}-{timestamp}-{suffix}"
    return candidate
