from __future__ import annotations

import re
import hashlib
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
    for document in documents:
        source_id = f"document:{document['id']}"
        path = reference_page_path(paths, dict(document))
        source_path_key = f"source_path:{document['source_path']}"
        exists_for_source = existing_sources.get(source_path_key) or existing_sources.get(source_id)
        if exists_for_source and not overwrite_generated:
            skipped.append(str(exists_for_source))
            continue
        markdown = render_reference_page(dict(document), source_id)
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

    lint_result = lint_wiki(paths) if not dry_run else None
    return {
        "documents": len(documents),
        "created": created,
        "updated": updated,
        "skipped": skipped,
        "dry_run": dry_run,
        "lint": lint_result,
    }


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
