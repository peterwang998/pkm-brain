from __future__ import annotations

import shutil
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .chunking import chunk_text
from .db import connection, dumps, init_db, loads, rows
from .embeddings import get_embedding_provider
from .indexes import delete_vectors, search_vectors, upsert_vectors
from .paths import BrainPaths
from .util import file_sha256, new_id, now_iso, slugify
from .wiki_proposals import create_wiki_proposal


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


GENERIC_CONTEXT_TERMS = {
    "about",
    "answer",
    "based",
    "brain",
    "context",
    "detail",
    "details",
    "evidence",
    "explain",
    "fetch",
    "from",
    "local",
    "memory",
    "only",
    "retrieve",
    "show",
    "summarize",
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
    "markdown_note": 3.0,
    "meeting_transcript": 2.0,
    "agent_session_log": -5.0,
}

RECENCY_MAX_BOOST = 2.0
RECENCY_HALF_LIFE_DAYS = 30.0


class BrainService:
    def __init__(self, paths: BrainPaths, prefer_model_embeddings: bool = False) -> None:
        self.paths = paths
        self.embedding_provider = get_embedding_provider(prefer_model_embeddings)

    def init_workspace(self) -> None:
        for directory in self.paths.directories():
            directory.mkdir(parents=True, exist_ok=True)
        init_db(self.paths.sqlite_path)
        if not self.paths.config_file.exists():
            self.paths.config_file.write_text(
                "brain_home: ~/brain\nembedding_model: BAAI/bge-small-en-v1.5\n",
                encoding="utf-8",
            )
        if not self.paths.golden_queries_file.exists():
            self.paths.golden_queries_file.write_text("[]\n", encoding="utf-8")

    def doctor(self) -> dict[str, Any]:
        return {
            "home": str(self.paths.home),
            "directories": {path.name: path.exists() for path in self.paths.directories()},
            "sqlite": self.paths.sqlite_path.exists(),
            "lancedb": self.paths.lancedb_path.exists(),
            "embedding_provider": self.embedding_provider.name,
        }

    def ingest(self, source: Path | None = None, dry_run: bool = False) -> IngestResult:
        self.init_workspace()
        source = source.expanduser().resolve() if source else self.paths.inbox
        candidates = sorted(path for path in source.rglob("*") if path.is_file() and not path.name.startswith("."))
        run_id = new_id("run")
        started = now_iso()
        errors: list[str] = []
        changed = 0
        skipped = 0
        chunks_created = 0
        documents_replaced = 0
        vector_rows: list[dict[str, Any]] = []
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
                    content_hash = file_sha256(path)
                    existing = conn.execute(
                        "SELECT id, source_type, title FROM documents WHERE content_hash = ?",
                        (content_hash,),
                    ).fetchone()
                    text = path.read_text(encoding="utf-8", errors="replace")
                    title = markdown_frontmatter_value(text, "title") or path.stem.replace("-", " ").replace("_", " ").strip() or path.name
                    if existing:
                        if source_type == "agent_session_log":
                            replaced = remove_superseded_agent_session_snapshots(conn, path, keep_document_id=existing["id"])
                            stale_vector_chunk_ids.extend(replaced.chunk_ids)
                            documents_replaced += replaced.documents
                        refresh_existing_document_metadata(conn, existing["id"], source_type, title, path)
                        skipped += 1
                        continue
                    document_id = new_id("doc")
                    ingested_at = now_iso()
                    if source_type == "agent_session_log":
                        replaced = remove_superseded_agent_session_snapshots(conn, path)
                        stale_vector_chunk_ids.extend(replaced.chunk_ids)
                        documents_replaced += replaced.documents
                    raw_path = self._copy_raw(path, source_type, ingested_at, content_hash)
                    conn.execute(
                        """
                        INSERT INTO documents(
                          id, source_type, title, source_path, raw_path, content_hash,
                          created_at, ingested_at, project, tags, sensitivity, version, status
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            document_id,
                            source_type,
                            title,
                            str(path),
                            str(raw_path),
                            content_hash,
                            ingested_at,
                            ingested_at,
                            None,
                            dumps([]),
                            "normal",
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
                        conn.execute(
                            """
                            INSERT INTO chunk_fts(chunk_id, title, text, heading_path, project, tags)
                            VALUES (?, ?, ?, ?, ?, ?)
                            """,
                            (chunk_id, title, chunk.text, chunk.heading_path, "", ""),
                        )
                        vector_rows.append(
                            {
                                "chunk_id": chunk_id,
                                "document_id": document_id,
                                "text": chunk.text,
                                "vector": self.embedding_provider.embed([chunk.text])[0],
                            }
                        )
                    changed += 1
                    chunks_created += len(doc_chunks)
                except Exception as exc:
                    errors.append(f"{path}: {exc}")
            delete_vectors(self.paths.lancedb_path, stale_vector_chunk_ids)
            embeddings_created = upsert_vectors(self.paths.lancedb_path, vector_rows)
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

        return IngestResult(run_id, len(candidates), changed, skipped, chunks_created, embeddings_created, errors, documents_replaced)

    def _copy_raw(self, source: Path, source_type: str, ingested_at: str, content_hash: str) -> Path:
        date = ingested_at[:10].split("-")
        target_dir = self.paths.raw / source_type / date[0] / date[1]
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"{slugify(source.stem)}-{content_hash[:12]}{source.suffix}"
        shutil.copy2(source, target)
        return target

    def search(self, query: str, limit: int = 10, debug: bool = False, caller: str = "cli") -> dict[str, Any]:
        self.init_workspace()
        lexical = self._search_fts(query, limit * 3)
        vector = search_vectors(self.paths.lancedb_path, self.embedding_provider, query, limit * 3)
        vector_debug = [
            {
                "chunk_id": row.get("chunk_id"),
                "document_id": row.get("document_id"),
                "distance": row.get("_distance"),
                "preview": str(row.get("text", ""))[:160],
            }
            for row in vector
        ]
        fused_ids = reciprocal_rank_fusion(
            [row["chunk_id"] for row in lexical],
            [row["chunk_id"] for row in vector],
        )
        selected_ids = fused_ids[:limit]
        selected = self._chunks_by_ids(selected_ids)
        event_id = new_id("retrieval")
        with connection(self.paths.sqlite_path) as conn:
            conn.execute(
                """
                INSERT INTO retrieval_events(
                  id, query, timestamp, caller, returned_chunk_ids, selected_chunk_ids, cited_chunk_ids, debug
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    query,
                    now_iso(),
                    caller,
                    dumps(fused_ids),
                    dumps(selected_ids),
                    dumps(selected_ids),
                    dumps({"lexical": lexical, "vector": vector_debug}) if debug else "{}",
                ),
            )
        return {
            "event_id": event_id,
            "query": query,
            "results": selected,
            "debug": {"lexical": lexical, "vector": vector_debug, "fused": fused_ids} if debug else None,
        }

    def retrieve_context(
        self,
        task: str,
        project: str | None = None,
        budget: int = 8000,
        debug: bool = False,
    ) -> dict[str, Any]:
        query = f"{project or ''} {task}".strip()
        chunk_candidates, fanout_debug = self._fanout_chunk_candidates(query, limit=60)
        reranked_chunks = rerank_chunks(query, chunk_candidates, fanout_debug)
        supporting_chunks = select_context_chunks(reranked_chunks, limit=8, budget=budget)
        wiki_pages = self.select_wiki_pages(query, supporting_chunks, limit=8)
        memories = self.active_memories(project)
        candidate_memories = self.candidate_memories(project)
        citations = dedupe_preserve_order(
            [row["chunk_id"] for row in supporting_chunks]
            + [source_id for page in wiki_pages for source_id in page.get("source_ids", [])]
        )
        event_id = new_id("retrieval")
        retrieval_debug = build_retrieval_debug(
            query,
            fanout_debug,
            supporting_chunks,
            reranked_chunks,
            wiki_pages,
            debug=debug,
        )
        with connection(self.paths.sqlite_path) as conn:
            conn.execute(
                """
                INSERT INTO retrieval_events(
                  id, query, timestamp, caller, returned_chunk_ids, selected_chunk_ids, cited_chunk_ids, debug
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    query,
                    now_iso(),
                    "retrieve_context",
                    dumps(fanout_debug["fused"]),
                    dumps([row["chunk_id"] for row in supporting_chunks]),
                    dumps(citations),
                    dumps(retrieval_debug),
                ),
            )
        return {
            "task": task,
            "project": project,
            "budget": budget,
            "active_memories": memories,
            "candidate_memories": candidate_memories,
            "relevant_wiki_pages": wiki_pages,
            "supporting_chunks": supporting_chunks,
            "citations": citations,
            "open_questions": [],
            "retrieval_event_id": event_id,
            "retrieval_debug": retrieval_debug,
        }

    def _fanout_chunk_candidates(self, query: str, limit: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        lexical = self._search_fts(query, limit)
        vector = search_vectors(self.paths.lancedb_path, self.embedding_provider, query, limit)
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
        return self._chunks_by_ids(candidate_ids), {
            "lexical": lexical,
            "vector": vector_debug,
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
        results.sort(key=lambda page: (-page["score"], page["page_type"] == "reference", page["title"]))
        return results[:limit]

    def select_wiki_pages(
        self,
        query: str,
        selected_chunks: list[dict[str, Any]],
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        self.init_workspace()
        terms = important_query_terms(query)
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
            source_ids = list(frontmatter.get("source_ids") or [])
            relative_path = str(path.relative_to(self.paths.wiki))
            is_agent_reference = "agent_session_log" in relative_path
            title_haystack = " ".join(
                [
                    title,
                    page_type,
                    relative_path,
                    " ".join(frontmatter.get("tags") or []),
                ]
            ).lower()
            body_haystack = body.lower()
            title_hits = sorted({term for term in terms if term in title_haystack})
            body_hits = sorted({term for term in terms if term in body_haystack})
            source_overlap = sorted(selected_sources.intersection(source_ids))

            if not title_hits and not (page_type == "reference" and source_overlap) and len(body_hits) < 2:
                continue
            if is_agent_reference and not agent_query and not source_overlap:
                continue

            score = 0.0
            reasons: list[str] = []
            if title_hits:
                boost = 5.0 * len(title_hits)
                score += boost
                reasons.append(f"title/path matched {', '.join(title_hits)} (+{boost:g})")
            if body_hits:
                boost = float(min(len(body_hits), 6))
                score += boost
                reasons.append(f"body matched {', '.join(body_hits[:6])} (+{boost:g})")
            if source_overlap:
                boost = 8.0 if page_type == "reference" else 2.0
                boost += min(6.0, float(len(source_overlap) * 2))
                score += boost
                reasons.append(f"shares selected source evidence (+{boost:g})")
            if page_type == "reference":
                score += 2.0
                reasons.append("reference page (+2)")
            elif page_type == "index":
                score -= 4.0
                reasons.append("index page penalty (-4)")
            if is_agent_reference and not agent_query:
                score -= 6.0
                reasons.append("agent-log reference penalty (-6)")

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
                }
            )

        results.sort(key=lambda page: (-page["score"], page["page_type"] != "reference", page["title"]))
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
        self.init_workspace()
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
        self.init_workspace()
        with connection(self.paths.sqlite_path) as conn:
            row = conn.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone()
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
        self.init_workspace()
        memory_id = new_id("mem")
        timestamp = now_iso()
        with connection(self.paths.sqlite_path) as conn:
            conn.execute(
                """
                INSERT INTO memories(id, memory_type, scope, content, confidence, source_ids, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (memory_id, memory_type, scope, content, confidence, dumps(sources), "proposed", timestamp, timestamp),
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

    def _set_memory_status(self, memory_id: str, status: str, reason: str | None = None) -> dict[str, Any]:
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
            row = conn.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone()
        if not row:
            raise ValueError(f"memory not found: {memory_id}")
        return row_to_memory(row)

    def propose_wiki_update(
        self,
        title: str,
        rationale: str,
        source_ids: list[str],
        changes: list[dict[str, Any]],
        confidence: float,
        author: str = "agent",
        source: str = "mcp",
    ) -> str:
        self.init_workspace()
        return create_wiki_proposal(
            self.paths,
            title=title,
            rationale=rationale,
            source_ids=source_ids,
            changes=changes,
            confidence=confidence,
            author=author,
            source=source,
        )

    def write_agent_session(
        self,
        summary: str,
        files_touched: list[str],
        commands_run: list[str],
        outcome: str,
        unresolved_issues: list[str],
    ) -> str:
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
            found = rows(
                conn,
                """
                SELECT chunk_id, bm25(chunk_fts) AS score
                FROM chunk_fts
                WHERE chunk_fts MATCH ?
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
                       c.created_at AS chunk_created_at,
                       d.id AS document_id, d.title, d.source_type, d.source_path, d.raw_path,
                       d.created_at AS document_created_at, d.ingested_at AS document_ingested_at
                FROM chunks c
                JOIN documents d ON d.id = c.document_id
                WHERE c.id IN ({placeholders})
                """,
                chunk_ids,
            )
        by_id = {row["chunk_id"]: dict(row) for row in found}
        return [by_id[chunk_id] for chunk_id in chunk_ids if chunk_id in by_id]


def row_to_memory(row: Any) -> dict[str, Any]:
    output = dict(row)
    output["source_ids"] = loads(output.get("source_ids"), [])
    return output


def rerank_chunks(query: str, chunks: list[dict[str, Any]], fanout_debug: dict[str, Any]) -> list[dict[str, Any]]:
    terms = important_query_terms(query)
    anchors = anchor_query_terms(terms, chunks)
    agent_query = is_agent_query(terms)
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

        title_hits = sorted({term for term in terms if term in title})
        heading_hits = sorted({term for term in terms if term in heading})
        text_hits = sorted({term for term in terms if term in text_lower})
        anchor_hits = sorted({term for term in anchors if term in title or term in heading or term in text_lower})
        title_heading_anchor_hits = sorted({term for term in anchors if term in title or term in heading})
        if title_hits:
            boost = 4.0 * len(title_hits)
            score += boost
            reasons.append(f"title matched {', '.join(title_hits)} (+{boost:g})")
        if heading_hits:
            boost = 2.5 * len(heading_hits)
            score += boost
            reasons.append(f"heading matched {', '.join(heading_hits)} (+{boost:g})")
        if text_hits and terms:
            boost = 6.0 * (len(text_hits) / len(terms))
            score += boost
            reasons.append(f"text covered {len(text_hits)}/{len(terms)} important terms (+{boost:.2f})")
        if anchors:
            if anchor_hits:
                boost = 3.0 * len(anchor_hits)
                score += boost
                reasons.append(f"entity anchor matched {', '.join(anchor_hits)} (+{boost:g})")
                if not title_heading_anchor_hits:
                    score -= 6.0
                    reasons.append(f"entity anchor absent from title/heading {', '.join(anchors)} (-6)")
            else:
                score -= 8.0
                reasons.append(f"missed entity anchor {', '.join(anchors)} (-8)")

        source_type = str(candidate.get("source_type") or "")
        source_weight = SOURCE_TYPE_WEIGHTS.get(source_type, 0.0)
        if source_type == "agent_session_log" and agent_query:
            source_weight = 1.5
        if source_weight:
            score += source_weight
            reasons.append(f"source_type {source_type} ({source_weight:+g})")

        noise_reasons = chunk_noise_reasons(candidate)
        if noise_reasons:
            if agent_query:
                penalty = 2.0
                score -= penalty
                reasons.append(f"minor noise penalty ({-penalty:g})")
            else:
                strong_noise = [reason for reason in noise_reasons if reason != "raw transcript chunk"]
                penalty = 4.0 if not strong_noise else 12.0 + (2.0 * len(strong_noise))
                score -= penalty
                reasons.append(f"noise penalty ({-penalty:g})")
                if strong_noise:
                    suppressed = True
                    suppress_reasons.extend(strong_noise)
        if source_type == "agent_session_log" and not agent_query and not suppressed:
            score -= 4.0
            reasons.append("agent log downranked for non-agent query (-4)")

        if not suppressed:
            recency_boost, recency_reason = recency_score(candidate)
            if recency_boost:
                score += recency_boost
                reasons.append(recency_reason)

        candidate["retrieval_score"] = round(score, 4)
        candidate["raw_context"] = raw_context_links(candidate)
        candidate["selection_reasons"] = reasons
        candidate["suppressed"] = suppressed
        candidate["suppress_reasons"] = suppress_reasons
        candidate["retrieval_noise_reasons"] = noise_reasons
        candidate["entity_anchor_title_heading_match"] = bool(title_heading_anchor_hits)
        scored.append(candidate)

    scored.sort(key=lambda row: (row.get("suppressed", False), -float(row["retrieval_score"]), row["chunk_index"]))
    return scored


def select_context_chunks(reranked_chunks: list[dict[str, Any]], limit: int, budget: int) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    remaining = max(budget, 1)
    eligible = [row for row in reranked_chunks if not row.get("suppressed")]
    if not eligible:
        eligible = reranked_chunks
    anchored = [row for row in eligible if row.get("entity_anchor_title_heading_match")]
    if anchored:
        eligible = anchored
    non_transcript = [
        row
        for row in eligible
        if "raw transcript chunk" not in row.get("retrieval_noise_reasons", [])
    ]
    if len(non_transcript) >= min(3, limit):
        eligible = non_transcript
    for row in eligible:
        if len(selected) >= limit:
            break
        token_count = int(row.get("token_count") or max(1, len(str(row.get("text") or "")) // 4))
        if selected and token_count > remaining:
            continue
        selected.append(row)
        remaining -= token_count
    return selected


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
        trace_markers = text_lower.count("event_msg") + text_lower.count("response_item")
        if trace_markers >= 2:
            reasons.append("tool/session trace")
    if looks_frontmatter_only(text):
        reasons.append("frontmatter-only chunk")
    if "no summary was captured" in text_lower and len(text_lower) < 600:
        reasons.append("empty generated summary")
    if "transcript" in heading or text_lower.startswith("## transcript"):
        reasons.append("raw transcript chunk")
    return dedupe_preserve_order(reasons)


def recency_score(chunk: dict[str, Any]) -> tuple[float, str]:
    timestamp = parse_iso_timestamp(str(chunk.get("document_created_at") or chunk.get("document_ingested_at") or ""))
    if not timestamp:
        return 0.0, ""
    age_days = max(0.0, (datetime.now(timezone.utc) - timestamp).total_seconds() / 86400.0)
    boost = RECENCY_MAX_BOOST * (RECENCY_HALF_LIFE_DAYS / (RECENCY_HALF_LIFE_DAYS + age_days))
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


def looks_frontmatter_only(text: str) -> bool:
    stripped = text.strip()
    if not stripped.startswith("---"):
        return False
    end = stripped.find("\n---", 3)
    if end == -1:
        return False
    tail = stripped[end + 4 :].strip()
    return not tail or len(tail) < 80


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
        payload["reranked_candidates"] = summarize_ranked_chunks(reranked_chunks, limit=30)
    else:
        payload["fanout_counts"] = {
            "lexical": len(fanout_debug.get("lexical", [])),
            "vector": len(fanout_debug.get("vector", [])),
            "fused": len(fanout_debug.get("fused", [])),
            "candidates": len(fanout_debug.get("candidate_ids", [])),
        }
    return payload


def summarize_ranked_chunks(chunks: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
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
            "preview": str(row.get("text", ""))[:160],
        }
        for row in chunks[:limit]
    ]


@dataclass
class ReplacedDocuments:
    documents: int
    chunk_ids: list[str]


def remove_superseded_agent_session_snapshots(conn: Any, path: Path, keep_document_id: str | None = None) -> ReplacedDocuments:
    query = """
        SELECT id, raw_path
        FROM documents
        WHERE source_type = 'agent_session_log'
          AND source_path = ?
    """
    params: list[Any] = [str(path)]
    if keep_document_id:
        query += " AND id != ?"
        params.append(keep_document_id)
    stale_documents = [dict(row) for row in conn.execute(query, params)]
    if not stale_documents:
        return ReplacedDocuments(0, [])

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
        chunk_placeholders = ",".join("?" for _ in stale_chunk_ids)
        conn.execute(f"DELETE FROM chunk_fts WHERE chunk_id IN ({chunk_placeholders})", stale_chunk_ids)
    conn.execute(f"DELETE FROM documents WHERE id IN ({placeholders})", document_ids)

    for row in stale_documents:
        raw_path = Path(str(row["raw_path"]))
        try:
            if raw_path.exists():
                raw_path.unlink()
        except OSError:
            pass
    return ReplacedDocuments(len(stale_documents), stale_chunk_ids)


def detect_source_type(path: Path) -> str | None:
    suffix = path.suffix.lower()
    if suffix == ".md":
        text = path.read_text(encoding="utf-8", errors="replace")[:2000].lower()
        if re.search(r"source_type:\s*['\"]?hyprnote_meeting", text) or "/documents/hyprnote/" in str(path):
            return "hyprnote_meeting"
        if re.search(r"source_type:\s*['\"]?agent_session_log", text) or "/agent_logs/" in str(path):
            return "agent_session_log"
        return "agent_session_log" if "commands" in text and "outcome" in text else "markdown_note"
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


def refresh_existing_document_metadata(conn: Any, document_id: str, source_type: str, title: str, path: Path) -> None:
    conn.execute(
        """
        UPDATE documents
        SET source_type = ?, title = ?, source_path = ?
        WHERE id = ?
        """,
        (source_type, title, str(path), document_id),
    )
    conn.execute(
        """
        UPDATE chunk_fts
        SET title = ?
        WHERE chunk_id IN (SELECT id FROM chunks WHERE document_id = ?)
        """,
        (title, document_id),
    )


def reciprocal_rank_fusion(*rankings: list[str], k: int = 60) -> list[str]:
    scores: dict[str, float] = {}
    for ranking in rankings:
        for rank, chunk_id in enumerate(ranking, start=1):
            scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (k + rank)
    return [chunk_id for chunk_id, _ in sorted(scores.items(), key=lambda item: item[1], reverse=True)]


def build_fts_query(query: str) -> str:
    terms = re.findall(r"[A-Za-z0-9_]+", query)
    return " OR ".join(f'"{term}"' for term in terms)


def query_terms(query: str) -> list[str]:
    stopwords = {
        "a",
        "an",
        "and",
        "are",
        "for",
        "how",
        "is",
        "of",
        "on",
        "or",
        "the",
        "to",
        "what",
        "with",
    }
    return [term.lower() for term in re.findall(r"[A-Za-z0-9_]+", query) if term.lower() not in stopwords]


def important_query_terms(query: str) -> list[str]:
    terms = [term for term in query_terms(query) if term not in GENERIC_CONTEXT_TERMS]
    return terms or query_terms(query)


def anchor_query_terms(terms: list[str], chunks: list[dict[str, Any]]) -> list[str]:
    anchors: list[str] = []
    for term in terms:
        if term in GENERIC_BUSINESS_TERMS or len(term) < 4:
            continue
        if any(term in str(row.get("title") or "").lower() for row in chunks):
            anchors.append(term)
    return anchors


def is_agent_query(terms: list[str]) -> bool:
    return bool(set(terms).intersection(AGENT_QUERY_TERMS))


def first_section(markdown: str, heading: str) -> str:
    match = re.search(rf"^##\s+{re.escape(heading)}\s*\n+(.*?)(?=^##\s+|\Z)", markdown, flags=re.MULTILINE | re.DOTALL)
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
