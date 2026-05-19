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
REFERENCE_FILENAME_SLUG_MAX_CHARS = 120
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
        if not source_ids and page_type not in {"index", "reference"} and page_id != "log" and status != "draft":
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
    match = re.match(r"\A---\n(.*?)\n---[ \t]*(?:\n|\Z)", text, flags=re.DOTALL)
    if not match:
        return None, text
    try:
        yaml_text = match.group(1)
        body = text[match.end() :]
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


def synthesize_wiki(
    paths: BrainPaths,
    dry_run: bool = False,
    overwrite_generated: bool = False,
    with_llm: bool = True,
    provider_name: str | None = None,
    require_llm: bool = False,
    auto_apply_confidence: float = 0.75,
    llm_source_limit: int = 12,
) -> dict[str, Any]:
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
    write_reference_pages(paths, document_dicts, existing_sources, created, updated, skipped, dry_run, overwrite_generated)

    llm_result: dict[str, Any] = {"enabled": with_llm, "status": "disabled"}
    if with_llm and document_dicts:
        try:
            from .wiki_compiler import compile_semantic_wiki

            llm_result = compile_semantic_wiki(
                paths,
                document_dicts,
                dry_run=dry_run,
                overwrite_generated=overwrite_generated,
                provider_name=provider_name,
                auto_apply_confidence=auto_apply_confidence,
                source_limit=llm_source_limit,
            )
            created.extend(llm_result.get("created", []))
            updated.extend(llm_result.get("updated", []))
            skipped.extend(llm_result.get("skipped", []))
        except Exception as exc:
            if require_llm:
                raise
            llm_result = {
                "enabled": True,
                "status": "skipped",
                "error": str(exc),
                "created": [],
                "updated": [],
                "skipped": [],
                "proposals": [],
            }

    index_action = write_index_page(paths, dry_run=dry_run, overwrite_generated=overwrite_generated)
    if index_action == "create":
        created.append(str(paths.wiki / "index.md"))
    elif index_action == "update":
        updated.append(str(paths.wiki / "index.md"))
    elif index_action == "skip":
        skipped.append(str(paths.wiki / "index.md"))

    log_action = write_log_page(paths, document_count=len(document_dicts), llm_result=llm_result, dry_run=dry_run)
    if log_action == "create":
        created.append(str(paths.wiki / "log.md"))
    elif log_action == "update":
        updated.append(str(paths.wiki / "log.md"))

    lint_result = lint_wiki(paths) if not dry_run else None
    return {
        "documents": len(documents),
        "created": created,
        "updated": updated,
        "skipped": skipped,
        "dry_run": dry_run,
        "llm_compile": llm_result,
        "lint": lint_result,
    }


def write_reference_pages(
    paths: BrainPaths,
    documents: list[dict[str, Any]],
    existing_sources: dict[str, Any],
    created: list[str],
    updated: list[str],
    skipped: list[str],
    dry_run: bool,
    overwrite_generated: bool,
) -> None:
    for document in documents:
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


def write_index_page(paths: BrainPaths, dry_run: bool, overwrite_generated: bool) -> str:
    path = paths.wiki / "index.md"
    markdown = render_index_page(collect_semantic_pages(paths))
    action = generated_write_action(path, overwrite_generated=overwrite_generated)
    if dry_run or action == "skip":
        return action
    path.write_text(markdown, encoding="utf-8")
    return action


def collect_semantic_pages(paths: BrainPaths) -> list[dict[str, Any]]:
    pages: list[dict[str, Any]] = []
    if not paths.wiki.exists():
        return pages
    for path in sorted(paths.wiki.rglob("*.md")):
        rel = path.relative_to(paths.wiki)
        if rel.parts[0] == "references" or rel.name in {"index.md", "log.md"}:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        frontmatter, body = parse_frontmatter(text)
        if not frontmatter:
            continue
        page_type = str(frontmatter.get("page_type") or "")
        expected_folder = SEMANTIC_FOLDERS.get(page_type)
        if not expected_folder or rel.parts[0] != expected_folder:
            continue
        pages.append(
            {
                "title": str(frontmatter.get("title") or path.stem),
                "page_type": page_type,
                "slug": path.stem,
                "summary": first_section_paragraph(body, "Summary") or "No summary yet.",
            }
        )
    return pages


def first_section_paragraph(markdown: str, section_name: str) -> str:
    pattern = re.compile(rf"^##\s+{re.escape(section_name)}\s*\n(.*?)(?=^##\s+|\Z)", flags=re.MULTILINE | re.DOTALL)
    match = pattern.search(markdown)
    if not match:
        return ""
    body = match.group(1).strip()
    for paragraph in re.split(r"\n\s*\n", body):
        paragraph = paragraph.strip()
        if paragraph and not paragraph.startswith("- "):
            return re.sub(r"\s+", " ", paragraph)
    return re.sub(r"\s+", " ", body.splitlines()[0].strip()) if body else ""


def index_related_items(compiled_pages: list[dict[str, Any]]) -> list[str]:
    items: list[str] = ["- [[log]]"]
    for page in sorted(compiled_pages, key=lambda item: (item["page_type"], item["title"]))[:20]:
        items.append(f"- [[{SEMANTIC_FOLDERS[page['page_type']]}/{page['slug']}]]")
    return items


def write_log_page(paths: BrainPaths, document_count: int, llm_result: dict[str, Any], dry_run: bool) -> str:
    path = paths.wiki / "log.md"
    action = generated_write_action(path)
    if action == "skip":
        return action
    existing_events = existing_log_events(path)
    event = wiki_log_event(document_count, llm_result)
    markdown = render_log_page(existing_events, event)
    if dry_run:
        return action
    path.write_text(markdown, encoding="utf-8")
    return action


def existing_log_events(path: Any) -> str:
    if not path.exists():
        return ""
    text = path.read_text(encoding="utf-8", errors="replace")
    _, body = parse_frontmatter(text)
    pattern = re.compile(r"^##\s+Events\s*\n(.*?)(?=^##\s+|\Z)", flags=re.MULTILINE | re.DOTALL)
    match = pattern.search(body)
    return match.group(1).strip() if match else ""


def wiki_log_event(document_count: int, llm_result: dict[str, Any]) -> str:
    timestamp = now_iso()
    status = llm_result.get("status") or "disabled"
    created = len(llm_result.get("created") or [])
    updated = len(llm_result.get("updated") or [])
    proposals = len(llm_result.get("proposals") or [])
    return (
        f"### [{timestamp[:10]}] wiki_synthesize | {status}\n\n"
        f"- Documents considered: {document_count}\n"
        f"- LLM semantic pages created: {created}\n"
        f"- LLM semantic pages updated: {updated}\n"
        f"- Review proposals created: {proposals}\n"
    )


def render_log_page(existing_events: str, event: str) -> str:
    timestamp = now_iso()[:10]
    frontmatter = {
        "title": "PKM Brain Wiki Log",
        "page_type": "timeline",
        "id": "log",
        "status": "active",
        "created_at": timestamp,
        "updated_at": timestamp,
        "source_ids": [],
        "related": [],
        "tags": ["wiki", "log"],
    }
    events = "\n\n".join(item for item in [existing_events, event.strip()] if item)
    return "\n".join(
        [
            "---",
            yaml.safe_dump(frontmatter, sort_keys=False).strip(),
            "---",
            "",
            GENERATED_MARKER,
            "",
            "# PKM Brain Wiki Log",
            "",
            "## Summary",
            "",
            "Chronological record of wiki synthesis, LLM compile, and maintenance events.",
            "",
            "## Key Points",
            "",
            "- `index.md` is the content-oriented navigation surface.",
            "- `log.md` records how the wiki changed over time.",
            "",
            "## Events",
            "",
            events or "- No events yet.",
            "",
            "## Current Status",
            "",
            "The wiki log is maintained automatically by `brain wiki synthesize`.",
            "",
            "## Source Evidence",
            "",
            "- Generated from local wiki synthesis runs.",
            "",
            "## Related Pages",
            "",
            "- [[index]]",
            "",
            "## Open Questions",
            "",
            "- Which synthesis events should be promoted into durable decision or open-loop pages?",
            "",
        ]
    )


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
    title_slug = slugify(title)[:REFERENCE_FILENAME_SLUG_MAX_CHARS].rstrip("-") or "untitled"
    filename = f"{title_slug}-{stable_id}.md"
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
