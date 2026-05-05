from __future__ import annotations

import shutil
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .chunking import chunk_text
from .db import connection, dumps, init_db, rows
from .embeddings import get_embedding_provider
from .indexes import search_vectors, upsert_vectors
from .paths import BrainPaths
from .util import file_sha256, new_id, now_iso, slugify


@dataclass
class IngestResult:
    run_id: str
    discovered: int
    changed: int
    skipped: int
    chunks_created: int
    embeddings_created: int
    errors: list[str]


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
        vector_rows: list[dict[str, Any]] = []

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
                        "SELECT id FROM documents WHERE content_hash = ?",
                        (content_hash,),
                    ).fetchone()
                    if existing:
                        skipped += 1
                        continue
                    text = path.read_text(encoding="utf-8", errors="replace")
                    document_id = new_id("doc")
                    ingested_at = now_iso()
                    raw_path = self._copy_raw(path, source_type, ingested_at, content_hash)
                    title = path.stem.replace("-", " ").replace("_", " ").strip() or path.name
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

        return IngestResult(run_id, len(candidates), changed, skipped, chunks_created, embeddings_created, errors)

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

    def retrieve_context(self, task: str, project: str | None = None, budget: int = 8000) -> dict[str, Any]:
        query = f"{project or ''} {task}".strip()
        wiki_pages = self.search_wiki_pages(query, limit=8)
        search_result = self.search(query, limit=8, caller="retrieve_context")
        memories = self.active_memories(project)
        citations = dedupe_preserve_order(
            [row["chunk_id"] for row in search_result["results"]]
            + [source_id for page in wiki_pages for source_id in page.get("source_ids", [])]
        )
        return {
            "task": task,
            "project": project,
            "budget": budget,
            "active_memories": memories,
            "relevant_wiki_pages": wiki_pages,
            "supporting_chunks": search_result["results"],
            "citations": citations,
            "open_questions": [],
            "retrieval_event_id": search_result["event_id"],
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

    def active_memories(self, project: str | None = None) -> list[dict[str, Any]]:
        self.init_workspace()
        with connection(self.paths.sqlite_path) as conn:
            query = "SELECT * FROM memories WHERE status = 'active'"
            params: tuple[Any, ...] = ()
            if project:
                query += " AND (scope = ? OR scope = 'global')"
                params = (f"project:{project}",)
            return [dict(row) for row in conn.execute(query, params)]

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
                SELECT c.id AS chunk_id, c.text, c.heading_path, c.chunk_index,
                       d.id AS document_id, d.title, d.source_type, d.source_path, d.raw_path
                FROM chunks c
                JOIN documents d ON d.id = c.document_id
                WHERE c.id IN ({placeholders})
                """,
                chunk_ids,
            )
        by_id = {row["chunk_id"]: dict(row) for row in found}
        return [by_id[chunk_id] for chunk_id in chunk_ids if chunk_id in by_id]


def detect_source_type(path: Path) -> str | None:
    suffix = path.suffix.lower()
    if suffix == ".md":
        text = path.read_text(encoding="utf-8", errors="replace")[:2000].lower()
        if "source_type: agent_session_log" in text or "/agent_logs/" in str(path):
            return "agent_session_log"
        return "agent_session_log" if "commands" in text and "outcome" in text else "markdown_note"
    if suffix == ".txt":
        return "meeting_transcript"
    if suffix in {".json", ".jsonl"}:
        return "agent_session_log"
    return None


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
