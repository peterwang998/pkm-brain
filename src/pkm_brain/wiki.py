from __future__ import annotations

import re
from typing import Any

import yaml

from .db import connection
from .paths import BrainPaths


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
