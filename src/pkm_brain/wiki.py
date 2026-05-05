from __future__ import annotations

import hashlib
import re
from typing import Any

import yaml

from .db import connection, rows
from .paths import BrainPaths
from .util import now_iso, slugify


ALLOWED_PAGE_TYPES = {
    "index",
    "project",
    "concept",
    "decision",
    "person",
    "open_loop",
    "timeline",
    "reference",
}
ALLOWED_STATUSES = {"draft", "active", "stale", "superseded", "archived"}
COMMON_SECTIONS = ["Summary", "Key Points", "Source Evidence", "Related Pages", "Open Questions"]
TYPE_SECTIONS = {
    "project": ["Current State", "Goals", "Decisions", "Open Loops", "Timeline"],
    "concept": ["Definition", "Why It Matters", "How It Works", "Related Decisions"],
    "decision": ["Context", "Decision", "Rationale", "Alternatives Considered", "Consequences"],
    "person": ["Role", "Relevant Projects", "Interaction History"],
    "open_loop": ["Question", "Current Understanding", "Needed Evidence", "Next Review"],
    "timeline": ["Events", "Current Status", "Source Evidence"],
    "reference": ["Notes", "Extracted Facts", "Source Evidence"],
}
GENERATED_MARKER = "<!-- generated-by: pkm-brain wiki-synthesis v1 -->"
SEMANTIC_FOLDERS = {
    "project": "projects",
    "concept": "concepts",
    "decision": "decisions",
    "person": "people",
    "open_loop": "open_loops",
    "timeline": "timelines",
}
COMPILED_PAGE_SPECS: list[dict[str, Any]] = [
    {
        "page_type": "project",
        "slug": "pkm-brain",
        "title": "PKM Brain",
        "patterns": [
            r"\bpkm[- ]brain\b",
            r"personal knowledge management",
            r"\bsecond brain\b",
            r"knowledge management tool",
        ],
        "summary": "PKM Brain is the local-first personal knowledge and agent memory system being built in this workspace.",
        "key_points": [
            "The system keeps raw captures immutable and derives searchable indexes, references, wiki pages, and memories from them.",
            "Agent session logs, working notes, and source documents are treated as evidence for durable compiled pages.",
            "The intended user-facing wiki is a linked synthesis layer, while reference pages remain provenance artifacts.",
        ],
        "related": [
            "concepts/wiki-synthesis-layer",
            "concepts/agent-session-capture",
            "concepts/local-first-agent-memory",
            "decisions/use-sqlite-for-canonical-metadata",
        ],
        "tags": ["pkm", "second-brain", "agent-memory"],
        "sections": {
            "Current State": "The project has a working local ingestion, retrieval, agent-log capture, reference page generation, and wiki linting pipeline.",
            "Goals": "Build a searchable second brain that agents can query for durable personal and project context.",
            "Decisions": "- [[decisions/use-sqlite-for-canonical-metadata]]\n- [[decisions/use-launchagent-for-agent-log-polling]]\n- [[decisions/maintain-wiki-as-compiled-markdown]]",
            "Open Loops": "- [[open_loops/nightly-ingest-catch-up-after-sleep]]\n- [[open_loops/improve-agent-log-distillation]]",
            "Timeline": "Use source evidence and ingestion logs for detailed chronology.",
        },
    },
    {
        "page_type": "concept",
        "slug": "wiki-synthesis-layer",
        "title": "Wiki Synthesis Layer",
        "patterns": [
            r"wiki synthesis",
            r"compiled wiki",
            r"human-readable",
            r"personal wikipedia",
            r"karpathy",
            r"llm wiki",
        ],
        "summary": "The wiki synthesis layer compiles raw notes and logs into durable, linked Markdown pages.",
        "definition": "A generated Markdown layer that summarizes current understanding across many sources and links related projects, concepts, decisions, and open loops.",
        "key_points": [
            "Compiled wiki pages should be human-readable and concept-oriented.",
            "Reference pages support provenance but should not be the main reading surface.",
            "Every generated semantic page needs source evidence so claims remain auditable.",
        ],
        "related": [
            "projects/pkm-brain",
            "concepts/agent-session-capture",
            "decisions/maintain-wiki-as-compiled-markdown",
        ],
        "tags": ["wiki", "synthesis", "markdown"],
        "sections": {
            "Why It Matters": "Compiled wiki pages let knowledge compound instead of being rediscovered from raw chunks during every query.",
            "How It Works": "The compiler reads ingested sources, identifies recurring topics, writes typed pages, links related pages, and records source evidence.",
            "Related Decisions": "- [[decisions/maintain-wiki-as-compiled-markdown]]",
        },
    },
    {
        "page_type": "concept",
        "slug": "agent-session-capture",
        "title": "Agent Session Capture",
        "patterns": [
            r"agent session",
            r"session logs",
            r"codex",
            r"claude",
            r"opencode",
            r"capture agents",
        ],
        "summary": "Agent session capture imports Codex, Claude, and OpenCode logs into the local brain as source material.",
        "definition": "A capture layer that polls local agent history, redacts sensitive payloads, and writes normalized Markdown artifacts into the inbox.",
        "key_points": [
            "Captured logs become evidence for later wiki synthesis and memory proposals.",
            "The capture format is versioned so redaction and parser improvements can force safe recapture.",
            "Captured sessions should be distilled before they become user-facing wiki knowledge.",
        ],
        "related": [
            "projects/pkm-brain",
            "concepts/wiki-synthesis-layer",
            "decisions/use-launchagent-for-agent-log-polling",
            "open_loops/improve-agent-log-distillation",
        ],
        "tags": ["agents", "capture", "logs"],
        "sections": {
            "Why It Matters": "Agent work contains decisions, preferences, repeated workflows, and unresolved issues that should survive context resets.",
            "How It Works": "A scheduled capture command discovers local agent logs, normalizes them into Markdown, ingests them, and makes them available for search and synthesis.",
            "Related Decisions": "- [[decisions/use-launchagent-for-agent-log-polling]]",
        },
    },
    {
        "page_type": "concept",
        "slug": "local-first-agent-memory",
        "title": "Local-First Agent Memory",
        "patterns": [
            r"local-first",
            r"agent memory",
            r"typed memory",
            r"memory layer",
            r"mcp",
        ],
        "summary": "Local-first agent memory keeps durable context on the user's machine while exposing retrieval tools to agents.",
        "definition": "A local storage and retrieval pattern where agents query durable personal context through tools instead of relying on chat history.",
        "key_points": [
            "Storage and indexes live under the local brain home.",
            "Agents interact through MCP and CLI surfaces.",
            "Memories should cite evidence and remain separate from raw documents and wiki pages.",
        ],
        "related": [
            "projects/pkm-brain",
            "concepts/hybrid-retrieval",
            "concepts/sqlite-metadata-store",
        ],
        "tags": ["memory", "agents", "local-first"],
        "sections": {
            "Why It Matters": "Local-first memory gives agents continuity without sending the whole corpus to a cloud service.",
            "How It Works": "The system stores documents, chunks, memories, retrieval logs, and wiki metadata locally, then exposes selected context through retrieval commands and MCP.",
            "Related Decisions": "- [[decisions/use-sqlite-for-canonical-metadata]]",
        },
    },
    {
        "page_type": "concept",
        "slug": "hybrid-retrieval",
        "title": "Hybrid Retrieval",
        "patterns": [
            r"hybrid retrieval",
            r"hybrid search",
            r"bm25",
            r"vector",
            r"rerank",
            r"rrf",
        ],
        "summary": "Hybrid retrieval combines lexical and vector search so agents can find both exact terms and semantically related material.",
        "definition": "A retrieval approach that fuses BM25-style keyword results with dense vector similarity results.",
        "key_points": [
            "Lexical search is useful for exact terms, filenames, tools, and identifiers.",
            "Vector search is useful for paraphrases and conceptual similarity.",
            "Compiled wiki pages and raw chunks should both be searchable.",
        ],
        "related": [
            "concepts/wiki-synthesis-layer",
            "concepts/local-first-agent-memory",
        ],
        "tags": ["retrieval", "search", "rag"],
        "sections": {
            "Why It Matters": "Personal corpora mix exact names with fuzzy concepts, so a single retrieval strategy is brittle.",
            "How It Works": "The V1 search path uses SQLite FTS for lexical search and LanceDB for vector search, then fuses candidates with reciprocal rank fusion.",
            "Related Decisions": "- [[decisions/use-sqlite-for-canonical-metadata]]",
        },
    },
    {
        "page_type": "concept",
        "slug": "sqlite-metadata-store",
        "title": "SQLite Metadata Store",
        "patterns": [
            r"\bsqlite\b",
            r"canonical metadata",
            r"metadata store",
            r"database",
        ],
        "summary": "SQLite is the local canonical store for metadata, chunks, memory records, retrieval events, and wiki page metadata.",
        "definition": "The inspectable local relational database that records the system's durable metadata and processing state.",
        "key_points": [
            "Raw artifacts remain on disk by default.",
            "SQLite tracks document and chunk metadata, ingestion runs, retrieval logs, memories, and wiki pages.",
            "Keeping metadata in SQLite makes the system debuggable with standard tools.",
        ],
        "related": [
            "concepts/local-first-agent-memory",
            "concepts/hybrid-retrieval",
            "decisions/use-sqlite-for-canonical-metadata",
        ],
        "tags": ["sqlite", "metadata", "storage"],
        "sections": {
            "Why It Matters": "A local metadata store keeps the system portable and easy to inspect during debugging.",
            "How It Works": "Ingestion writes document, chunk, index, memory, retrieval, and wiki records into SQLite while raw files stay in the filesystem.",
            "Related Decisions": "- [[decisions/use-sqlite-for-canonical-metadata]]",
        },
    },
    {
        "page_type": "decision",
        "slug": "use-sqlite-for-canonical-metadata",
        "title": "Use SQLite For Canonical Metadata",
        "patterns": [
            r"\bsqlite\b",
            r"canonical metadata",
            r"metadata store",
        ],
        "summary": "Use SQLite as the canonical local metadata database for V1.",
        "key_points": [
            "SQLite is simple to install, inspect, back up, and rebuild.",
            "Raw artifacts remain on the filesystem rather than being stored primarily inside SQLite.",
            "Vector indexes can remain separate while SQLite owns canonical metadata.",
        ],
        "related": [
            "concepts/sqlite-metadata-store",
            "concepts/local-first-agent-memory",
            "projects/pkm-brain",
        ],
        "tags": ["decision", "sqlite", "storage"],
        "sections": {
            "Context": "The system needs a local, low-operational-burden source of truth for metadata and pipeline state.",
            "Decision": "Use SQLite as the canonical metadata store for V1.",
            "Rationale": "SQLite is portable, inspectable, and sufficient for a local single-user workload.",
            "Alternatives Considered": "- Postgres with pgvector\n- SQLite-only retrieval with vector extensions\n- SQLite plus a separate vector index",
            "Consequences": "The system remains simple locally, but high-scale vector search may continue to use a separate index.",
        },
    },
    {
        "page_type": "decision",
        "slug": "use-launchagent-for-agent-log-polling",
        "title": "Use LaunchAgent For Agent Log Polling",
        "patterns": [
            r"launchagent",
            r"scheduled polling",
            r"every 10 min",
            r"every 10 minutes",
            r"agent-log-ingest",
        ],
        "summary": "Use a macOS LaunchAgent to poll local agent session logs on a recurring schedule.",
        "key_points": [
            "The installed schedule polls for Codex, Claude, and OpenCode session logs.",
            "This is scheduled polling, not a general filesystem watcher.",
            "Nightly ingestion can use a separate LaunchAgent if needed.",
        ],
        "related": [
            "concepts/agent-session-capture",
            "open_loops/nightly-ingest-catch-up-after-sleep",
            "projects/pkm-brain",
        ],
        "tags": ["decision", "launchagent", "automation"],
        "sections": {
            "Context": "Agent logs change outside the PKM tool and need to be captured without manual commands.",
            "Decision": "Use a macOS LaunchAgent to run scheduled local polling for agent logs.",
            "Rationale": "LaunchAgent is native to macOS, survives shell sessions, and is appropriate for a laptop-local background job.",
            "Alternatives Considered": "- Manual capture commands\n- Filesystem hooks\n- A long-running custom daemon",
            "Consequences": "The system captures recent logs automatically, but missed runs after sleep need explicit catch-up handling.",
        },
    },
    {
        "page_type": "decision",
        "slug": "maintain-wiki-as-compiled-markdown",
        "title": "Maintain Wiki As Compiled Markdown",
        "patterns": [
            r"compiled markdown",
            r"personal wikipedia",
            r"human-readable",
            r"wiki synthesis",
            r"llm wiki",
            r"karpathy",
        ],
        "summary": "Treat the wiki as the durable compiled Markdown layer, not as raw source dumps.",
        "key_points": [
            "Semantic pages should group related concepts across sources.",
            "Reference pages are useful for provenance but are not the main wiki surface.",
            "Compiled pages should link to related concepts, projects, decisions, and open loops.",
        ],
        "related": [
            "concepts/wiki-synthesis-layer",
            "concepts/agent-session-capture",
            "projects/pkm-brain",
        ],
        "tags": ["decision", "wiki", "markdown"],
        "sections": {
            "Context": "The project needs a wiki that feels like a personal knowledge base rather than a list of captured files.",
            "Decision": "Maintain semantic Markdown pages as the primary wiki layer and keep reference pages as supporting provenance.",
            "Rationale": "This follows the Karpathy LLM Wiki pattern where knowledge compounds in maintained Markdown pages.",
            "Alternatives Considered": "- Use only hybrid search over raw chunks\n- Generate one summary page per source\n- Keep all context in agent chat history",
            "Consequences": "The wiki compiler must update links, indexes, and source-backed semantic pages instead of only generating references.",
        },
    },
    {
        "page_type": "open_loop",
        "slug": "nightly-ingest-catch-up-after-sleep",
        "title": "Nightly Ingest Catch-Up After Sleep",
        "patterns": [
            r"nightly",
            r"after waking",
            r"after sleep",
            r"missed one run",
            r"catch-up",
        ],
        "summary": "The nightly ingest job should eventually catch up when the laptop wakes after missing a scheduled run.",
        "key_points": [
            "A LaunchAgent can schedule a nightly job when the laptop is awake.",
            "Wake-after-missed-run behavior needs explicit design.",
            "This has been tracked as follow-up work.",
        ],
        "related": [
            "decisions/use-launchagent-for-agent-log-polling",
            "projects/pkm-brain",
        ],
        "tags": ["open-loop", "automation", "launchagent"],
        "sections": {
            "Question": "How should nightly ingestion run after wake if the laptop was asleep during the scheduled time?",
            "Current Understanding": "A regular LaunchAgent can schedule the nightly run, but missed-run catch-up needs additional state or a self-healing check.",
            "Needed Evidence": "Confirm macOS launchd behavior for missed StartCalendarInterval jobs and decide whether to add a custom last-run check.",
            "Next Review": "Review before implementing the nightly maintenance LaunchAgent.",
        },
    },
    {
        "page_type": "open_loop",
        "slug": "improve-agent-log-distillation",
        "title": "Improve Agent Log Distillation",
        "patterns": [
            r"agent logs",
            r"raw session",
            r"distill",
            r"distillation",
            r"session dump",
        ],
        "summary": "Captured agent logs need stronger distillation before they become high-quality wiki knowledge.",
        "key_points": [
            "Raw session logs are noisy and may include tool output, repeated context, and operational details.",
            "The compiled wiki should extract durable concepts, decisions, and open loops instead of exposing raw dumps.",
            "Redaction and source evidence must remain part of the capture pipeline.",
        ],
        "related": [
            "concepts/agent-session-capture",
            "concepts/wiki-synthesis-layer",
            "projects/pkm-brain",
        ],
        "tags": ["open-loop", "agents", "distillation"],
        "sections": {
            "Question": "How should agent sessions be summarized into durable knowledge without carrying forward irrelevant noise?",
            "Current Understanding": "The current system captures and indexes sessions, but semantic extraction is still template and keyword driven.",
            "Needed Evidence": "Evaluate generated pages against real session logs and identify missing concepts, decisions, and false positives.",
            "Next Review": "Review after several automated capture and wiki synthesis runs.",
        },
    },
]


def lint_wiki(paths: BrainPaths) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    pages: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    for path in sorted(paths.wiki.rglob("*.md")):
        rel = path.relative_to(paths.wiki)
        text = path.read_text(encoding="utf-8", errors="replace")
        frontmatter, body = parse_frontmatter(text)
        if frontmatter is None:
            errors.append(f"{rel}: missing or invalid YAML frontmatter")
            continue
        page_id = str(frontmatter.get("id", ""))
        page_type = str(frontmatter.get("page_type", ""))
        status = str(frontmatter.get("status", ""))
        title = str(frontmatter.get("title", ""))
        if not page_id:
            errors.append(f"{rel}: missing id")
        elif page_id in seen_ids:
            errors.append(f"{rel}: duplicate id {page_id}")
        seen_ids.add(page_id)
        if page_type not in ALLOWED_PAGE_TYPES:
            errors.append(f"{rel}: invalid page_type {page_type!r}")
        if status not in ALLOWED_STATUSES:
            errors.append(f"{rel}: invalid status {status!r}")
        sections = set(re.findall(r"^##\s+(.+?)\s*$", body, flags=re.MULTILINE))
        for section in COMMON_SECTIONS:
            if section not in sections:
                errors.append(f"{rel}: missing common section ## {section}")
        for section in TYPE_SECTIONS.get(page_type, []):
            if section not in sections:
                errors.append(f"{rel}: missing {page_type} section ## {section}")
        source_ids = frontmatter.get("source_ids") or []
        if page_type == "decision" and not source_ids:
            errors.append(f"{rel}: decision page requires source_ids")
        if not source_ids and page_type not in {"index", "reference"} and status != "draft":
            warnings.append(f"{rel}: active non-reference page has no source_ids")
        pages.append(
            {
                "id": page_id,
                "title": title,
                "page_type": page_type,
                "status": status,
                "path": str(path),
                "source_ids": source_ids,
                "related": frontmatter.get("related") or [],
                "tags": frontmatter.get("tags") or [],
                "created_at": frontmatter.get("created_at"),
                "updated_at": frontmatter.get("updated_at"),
            }
        )

    sync_wiki_pages(paths, pages)
    return {"pages": len(pages), "errors": errors, "warnings": warnings}


def parse_frontmatter(text: str) -> tuple[dict[str, Any] | None, str]:
    if not text.startswith("---\n"):
        return None, text
    try:
        _, yaml_text, body = text.split("---", 2)
        parsed = yaml.safe_load(yaml_text) or {}
        return parsed, body
    except Exception:
        return None, text


def sync_wiki_pages(paths: BrainPaths, pages: list[dict[str, Any]]) -> None:
    with connection(paths.sqlite_path) as conn:
        for page in pages:
            if not page.get("id"):
                continue
            conn.execute(
                """
                INSERT INTO wiki_pages(id, title, page_type, status, path, source_ids, related, tags, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                  title = excluded.title,
                  page_type = excluded.page_type,
                  status = excluded.status,
                  path = excluded.path,
                  source_ids = excluded.source_ids,
                  related = excluded.related,
                  tags = excluded.tags,
                  created_at = excluded.created_at,
                  updated_at = excluded.updated_at
                """,
                (
                    page["id"],
                    page["title"],
                    page["page_type"],
                    page["status"],
                    page["path"],
                    yaml.safe_dump(page["source_ids"]),
                    yaml.safe_dump(page["related"]),
                    yaml.safe_dump(page["tags"]),
                    str(page.get("created_at") or ""),
                    str(page.get("updated_at") or ""),
                ),
            )


def synthesize_wiki(paths: BrainPaths, dry_run: bool = False, overwrite_generated: bool = False) -> dict[str, Any]:
    paths.wiki.mkdir(parents=True, exist_ok=True)
    existing_sources = existing_generated_sources(paths)
    created: list[str] = []
    skipped: list[str] = []
    updated: list[str] = []
    with connection(paths.sqlite_path) as conn:
        documents = rows(
            conn,
            """
            SELECT d.*,
                   COUNT(c.id) AS chunk_count,
                   GROUP_CONCAT(c.text, '\n\n---CHUNK---\n\n') AS chunk_texts
            FROM documents d
            LEFT JOIN chunks c ON c.document_id = d.id
            WHERE d.ingested_at = (
              SELECT MAX(d2.ingested_at)
              FROM documents d2
              WHERE d2.source_path = d.source_path
            )
            GROUP BY d.id
            ORDER BY d.ingested_at DESC
            """,
        )
    document_dicts = [dict(document) for document in documents]
    for document in document_dicts:
        source_id = f"document:{document['id']}"
        path = reference_page_path(paths, document)
        source_path_key = f"source_path:{document['source_path']}"
        exists_for_source = existing_sources.get(source_path_key) or existing_sources.get(source_id)
        if exists_for_source and not overwrite_generated:
            skipped.append(str(exists_for_source))
            continue
        markdown = render_reference_page(document, source_id)
        if dry_run:
            if exists_for_source:
                updated.append(str(exists_for_source))
            else:
                created.append(str(path))
            continue
        target = exists_for_source or path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(markdown, encoding="utf-8")
        if exists_for_source:
            updated.append(str(target))
        else:
            created.append(str(target))

    compiled_pages = compile_semantic_pages(paths, document_dicts)
    for page in compiled_pages:
        path = page["path"]
        markdown = page["markdown"]
        action = generated_write_action(path, overwrite_generated=overwrite_generated)
        if action == "skip":
            skipped.append(str(path))
            continue
        if dry_run:
            if action == "update":
                updated.append(str(path))
            else:
                created.append(str(path))
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(markdown, encoding="utf-8")
        if action == "update":
            updated.append(str(path))
        else:
            created.append(str(path))

    lint_result = lint_wiki(paths) if not dry_run else None
    return {
        "documents": len(documents),
        "created": created,
        "updated": updated,
        "skipped": skipped,
        "dry_run": dry_run,
        "lint": lint_result,
    }


def compile_semantic_pages(paths: BrainPaths, documents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    pages: list[dict[str, Any]] = []
    compiled: list[dict[str, Any]] = []
    for spec in COMPILED_PAGE_SPECS:
        evidence = matching_documents(spec, documents)
        if not evidence:
            continue
        page = render_compiled_page(spec, evidence)
        pages.append({"path": semantic_page_path(paths, spec["page_type"], spec["slug"]), "markdown": page})
        compiled.append(
            {
                "title": spec["title"],
                "page_type": spec["page_type"],
                "slug": spec["slug"],
                "summary": spec["summary"],
            }
        )

    pages.append({"path": paths.wiki / "index.md", "markdown": render_index_page(compiled)})
    return pages


def matching_documents(spec: dict[str, Any], documents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    for document in documents:
        haystack = document_search_text(document)
        if any(re.search(pattern, haystack, flags=re.IGNORECASE) for pattern in spec["patterns"]):
            matches.append(document)
    return matches


def document_search_text(document: dict[str, Any]) -> str:
    return "\n".join(
        [
            str(document.get("title") or ""),
            str(document.get("source_type") or ""),
            str(document.get("source_path") or ""),
            str(document.get("chunk_texts") or ""),
        ]
    )


def semantic_page_path(paths: BrainPaths, page_type: str, slug: str) -> Any:
    folder = SEMANTIC_FOLDERS[page_type]
    return paths.wiki / folder / f"{slug}.md"


def generated_write_action(path: Any, overwrite_generated: bool = False) -> str:
    if not path.exists():
        return "create"
    text = path.read_text(encoding="utf-8", errors="replace")
    if GENERATED_MARKER in text or overwrite_generated:
        return "update"
    return "skip"


def render_compiled_page(spec: dict[str, Any], evidence: list[dict[str, Any]]) -> str:
    page_type = spec["page_type"]
    slug = spec["slug"]
    title = spec["title"]
    timestamp = now_iso()[:10]
    source_ids = [f"document:{document['id']}" for document in evidence]
    frontmatter = {
        "title": title,
        "page_type": page_type,
        "id": f"{page_type}-{slug}",
        "status": "active",
        "created_at": timestamp,
        "updated_at": timestamp,
        "source_ids": source_ids,
        "related": spec.get("related") or [],
        "tags": spec.get("tags") or [],
    }
    lines = [
        "---",
        yaml.safe_dump(frontmatter, sort_keys=False).strip(),
        "---",
        "",
        GENERATED_MARKER,
        "",
        f"# {title}",
        "",
        "## Summary",
        "",
        spec["summary"],
        "",
        "## Key Points",
        "",
        *markdown_items(spec.get("key_points") or []),
        "",
    ]
    for section in TYPE_SECTIONS.get(page_type, []):
        if section == "Source Evidence":
            continue
        body = section_body(spec, section)
        lines.extend([f"## {section}", "", body, ""])
    lines.extend(
        [
            "## Source Evidence",
            "",
            *source_evidence_items(evidence),
            "",
            "## Related Pages",
            "",
            *related_page_items(spec.get("related") or []),
            "",
            "## Open Questions",
            "",
            *markdown_items(open_questions_for(spec)),
            "",
        ]
    )
    return "\n".join(lines)


def section_body(spec: dict[str, Any], section: str) -> str:
    sections = spec.get("sections") or {}
    if section in sections:
        return sections[section]
    if section == "Definition":
        return spec.get("definition") or spec["summary"]
    return "No synthesized content yet."


def markdown_items(items: list[str]) -> list[str]:
    if not items:
        return ["- None yet."]
    return [item if item.startswith("- ") else f"- {item}" for item in items]


def source_evidence_items(evidence: list[dict[str, Any]]) -> list[str]:
    items: list[str] = []
    for document in evidence[:12]:
        source_id = f"document:{document['id']}"
        title = document.get("title") or document["id"]
        items.append(f"- `{source_id}`: {title}")
    omitted = len(evidence) - len(items)
    if omitted > 0:
        items.append(f"- {omitted} additional source document(s) omitted from this page.")
    return items or ["- None yet."]


def related_page_items(related: list[str]) -> list[str]:
    if not related:
        return ["- None yet."]
    return [f"- [[{page}]]" for page in related]


def open_questions_for(spec: dict[str, Any]) -> list[str]:
    if spec["page_type"] == "open_loop":
        return ["Track until the open loop is resolved or superseded."]
    return ["What source evidence would change or refine this page?"]


def render_index_page(compiled_pages: list[dict[str, Any]]) -> str:
    timestamp = now_iso()[:10]
    frontmatter = {
        "title": "PKM Brain Wiki Index",
        "page_type": "index",
        "id": "index",
        "status": "active",
        "created_at": timestamp,
        "updated_at": timestamp,
        "source_ids": [],
        "related": [],
        "tags": ["index", "wiki"],
    }
    by_type: dict[str, list[dict[str, Any]]] = {}
    for page in compiled_pages:
        by_type.setdefault(page["page_type"], []).append(page)
    lines = [
        "---",
        yaml.safe_dump(frontmatter, sort_keys=False).strip(),
        "---",
        "",
        GENERATED_MARKER,
        "",
        "# PKM Brain Wiki Index",
        "",
        "## Summary",
        "",
        "Entry point for the compiled PKM Brain wiki.",
        "",
        "## Key Points",
        "",
        "- Semantic pages are the main reading layer.",
        "- Reference pages preserve source provenance.",
        "- Use the links below to navigate projects, concepts, decisions, and open loops.",
        "",
        "## Pages",
        "",
    ]
    if compiled_pages:
        for page_type in ["project", "concept", "decision", "open_loop", "person", "timeline"]:
            pages = sorted(by_type.get(page_type, []), key=lambda page: page["title"])
            if not pages:
                continue
            lines.extend([f"### {page_type.replace('_', ' ').title()}s", ""])
            for page in pages:
                link = f"{SEMANTIC_FOLDERS[page_type]}/{page['slug']}"
                lines.append(f"- [[{link}]] - {page['summary']}")
            lines.append("")
    else:
        lines.extend(["- No semantic pages compiled yet.", ""])
    lines.extend(
        [
            "## Source Evidence",
            "",
            "- Index page generated from compiled wiki page metadata.",
            "",
            "## Related Pages",
            "",
            *index_related_items(compiled_pages),
            "",
            "## Open Questions",
            "",
            "- Which missing pages should be promoted during the next synthesis pass?",
            "",
        ]
    )
    return "\n".join(lines)


def index_related_items(compiled_pages: list[dict[str, Any]]) -> list[str]:
    if not compiled_pages:
        return ["- None yet."]
    items: list[str] = []
    for page in sorted(compiled_pages, key=lambda item: (item["page_type"], item["title"]))[:20]:
        items.append(f"- [[{SEMANTIC_FOLDERS[page['page_type']]}/{page['slug']}]]")
    return items


def existing_generated_sources(paths: BrainPaths) -> dict[str, Any]:
    found: dict[str, Any] = {}
    for path in paths.wiki.rglob("*.md"):
        text = path.read_text(encoding="utf-8", errors="replace")
        if GENERATED_MARKER not in text:
            continue
        frontmatter, _ = parse_frontmatter(text)
        if not frontmatter:
            continue
        for source_id in frontmatter.get("source_ids") or []:
            found[str(source_id)] = path
        if frontmatter.get("source_path"):
            found[f"source_path:{frontmatter['source_path']}"] = path
    return found


def reference_page_path(paths: BrainPaths, document: dict[str, Any]) -> Any:
    title = document["title"] or document["id"]
    stable_id = hashlib.sha256(str(document["source_path"]).encode("utf-8")).hexdigest()[:8]
    filename = f"{slugify(title)}-{stable_id}.md"
    return paths.wiki / "references" / document["source_type"] / filename


def render_reference_page(document: dict[str, Any], source_id: str) -> str:
    title = document["title"] or document["id"]
    page_id = f"reference-{slugify(title)}-{str(document['id'])[-8:]}"
    chunk_texts = split_chunk_texts(document.get("chunk_texts") or "")
    excerpts = [clean_excerpt(text) for text in chunk_texts[:3] if clean_excerpt(text)]
    timestamp = now_iso()[:10]
    frontmatter = {
        "title": title,
        "page_type": "reference",
        "id": page_id,
        "status": "active",
        "created_at": timestamp,
        "updated_at": timestamp,
        "source_ids": [source_id],
        "source_path": document["source_path"],
        "related": [],
        "tags": [document["source_type"]],
    }
    lines = [
        "---",
        yaml.safe_dump(frontmatter, sort_keys=False).strip(),
        "---",
        "",
        GENERATED_MARKER,
        "",
        f"# {title}",
        "",
        "## Summary",
        "",
        f"Reference page synthesized from `{document['source_type']}` source `{source_id}`.",
        "",
        "## Key Points",
        "",
        f"- Source type: `{document['source_type']}`",
        f"- Source path: `{document['source_path']}`",
        f"- Raw path: `{document['raw_path']}`",
        f"- Chunk count: `{document['chunk_count']}`",
        f"- Ingested at: `{document['ingested_at']}`",
        "",
        "## Source Evidence",
        "",
        f"- `{source_id}`",
        "",
        "## Related Pages",
        "",
        "- None yet.",
        "",
        "## Open Questions",
        "",
        "- None yet.",
        "",
        "## Notes",
        "",
    ]
    if excerpts:
        for excerpt in excerpts:
            lines.append(f"- {excerpt}")
    else:
        lines.append("- No notes extracted yet.")
    lines.extend(
        [
            "",
            "## Extracted Facts",
            "",
            "- No extracted facts yet.",
            "",
        ]
    )
    return "\n".join(lines)


def split_chunk_texts(value: str) -> list[str]:
    if not value:
        return []
    return value.split("\n\n---CHUNK---\n\n")


def clean_excerpt(text: str, max_chars: int = 500) -> str:
    text = re.sub(r"\A---\n.*?\n---\n", "", text, flags=re.DOTALL)
    text = text.replace(GENERATED_MARKER, "")
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= max_chars:
        return text
    return f"{text[:max_chars]}... [truncated]"
