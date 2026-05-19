from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .llm import get_provider
from .paths import BrainPaths
from .util import slugify
from .wiki import (
    GENERATED_MARKER,
    SEMANTIC_FOLDERS,
    TYPE_SECTIONS,
    clean_excerpt,
    first_section_paragraph,
    generated_write_action,
    parse_frontmatter,
    render_compiled_page,
    semantic_page_path,
)
from .wiki_proposals import create_wiki_proposal, parse_json_object

SEMANTIC_PAGE_TYPES = set(SEMANTIC_FOLDERS)


def compile_semantic_wiki(
    paths: BrainPaths,
    documents: list[dict[str, Any]],
    dry_run: bool = False,
    overwrite_generated: bool = False,
    provider_name: str | None = None,
    auto_apply_confidence: float = 0.75,
    source_limit: int = 12,
) -> dict[str, Any]:
    provider = get_provider(provider_name)
    source_documents = compiler_documents(documents[:source_limit])
    if not source_documents:
        return llm_result("no_sources", provider, [], [], [], [])

    document_by_source_id = {document["source_id"]: document["raw_document"] for document in compiler_documents(documents)}
    prompt = semantic_compile_prompt(source_documents, existing_semantic_pages(paths))
    parsed = parse_json_object(provider.complete(prompt))
    raw_pages = parsed.get("pages") or []
    if not isinstance(raw_pages, list) or not raw_pages:
        return {
            **llm_result("no_changes", provider, [], [], [], []),
            "rationale": str(parsed.get("rationale") or ""),
        }

    created: list[str] = []
    updated: list[str] = []
    skipped: list[str] = []
    proposal_changes: list[dict[str, Any]] = []
    errors: list[str] = []

    for raw_page in raw_pages:
        try:
            spec = normalize_page_spec(raw_page)
            evidence = page_evidence(spec, document_by_source_id)
            if not evidence:
                errors.append(f"{spec['page_type']}/{spec['slug']}: no matching source evidence")
                continue
            path = semantic_page_path(paths, spec["page_type"], spec["slug"])
            markdown = render_compiled_page(spec, evidence)
            confidence = float(spec.get("confidence", 0.0))
            action = generated_write_action(path, overwrite_generated=overwrite_generated)
            if action != "skip" and confidence >= auto_apply_confidence:
                if dry_run:
                    (updated if action == "update" else created).append(str(path))
                    continue
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(markdown, encoding="utf-8")
                (updated if action == "update" else created).append(str(path))
                continue
            skipped.append(str(path))
            proposal_changes.append(page_proposal_change(path, paths.wiki, action, markdown, spec, evidence))
        except Exception as exc:
            errors.append(str(exc))

    proposals = []
    if proposal_changes:
        source_ids = sorted({source_id for change in proposal_changes for source_id in change.get("source_ids", [])})
        if dry_run:
            proposals.append({"status": "planned", "items": len(proposal_changes), "source_ids": source_ids})
        else:
            batch_id = create_wiki_proposal(
                paths,
                title=str(parsed.get("title") or "LLM semantic wiki compile"),
                rationale=str(parsed.get("rationale") or "LLM-generated semantic wiki updates from source documents."),
                source_ids=source_ids,
                changes=proposal_changes,
                confidence=min(float(parsed.get("confidence", auto_apply_confidence)), auto_apply_confidence),
                author=f"llm:{provider.name}",
                source="wiki_compiler",
                status="needs_interview",
            )
            proposals.append({"batch_id": batch_id, "items": len(proposal_changes), "source_ids": source_ids})

    status = "ok"
    if errors and not (created or updated or proposals):
        status = "failed"
    elif errors:
        status = "ok_with_errors"
    return {
        **llm_result(status, provider, created, updated, skipped, proposals),
        "errors": errors,
        "rationale": str(parsed.get("rationale") or ""),
    }


def llm_result(
    status: str,
    provider: Any,
    created: list[str],
    updated: list[str],
    skipped: list[str],
    proposals: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "enabled": True,
        "status": status,
        "provider": provider.name,
        "model": provider.model,
        "created": created,
        "updated": updated,
        "skipped": skipped,
        "proposals": proposals,
    }


def compiler_documents(documents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output = []
    for document in documents:
        source_id = f"document:{document['id']}"
        chunk_texts = split_chunks(document.get("chunk_texts") or "")
        preview = "\n\n".join(clean_excerpt(text, max_chars=900) for text in chunk_texts[:3])
        if not preview:
            preview = clean_excerpt(str(document.get("title") or ""), max_chars=900)
        output.append(
            {
                "source_id": source_id,
                "title": document.get("title") or document["id"],
                "source_type": document.get("source_type"),
                "source_path": document.get("source_path"),
                "ingested_at": document.get("ingested_at"),
                "preview": preview[:3000],
                "raw_document": document,
            }
        )
    return output


def split_chunks(value: str) -> list[str]:
    if not value:
        return []
    return value.split("\n\n---CHUNK---\n\n")


def existing_semantic_pages(paths: BrainPaths) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    if not paths.wiki.exists():
        return output
    for path in sorted(paths.wiki.rglob("*.md")):
        rel = path.relative_to(paths.wiki)
        if rel.parts[0] == "references" or rel.name in {"index.md", "log.md"}:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        frontmatter, body = parse_frontmatter(text)
        if not frontmatter:
            continue
        page_type = str(frontmatter.get("page_type") or "")
        if page_type not in SEMANTIC_PAGE_TYPES:
            continue
        output.append(
            {
                "target_path": str(rel),
                "title": frontmatter.get("title") or path.stem,
                "page_type": page_type,
                "summary": first_section_paragraph(body, "Summary"),
                "source_ids": frontmatter.get("source_ids") or [],
                "related": frontmatter.get("related") or [],
                "generated": GENERATED_MARKER in text,
            }
        )
    return output[:40]


def semantic_compile_prompt(source_documents: list[dict[str, Any]], existing_pages: list[dict[str, Any]]) -> str:
    prompt_sources = [{key: value for key, value in document.items() if key != "raw_document"} for document in source_documents]
    schema = {
        "title": "short batch title",
        "rationale": "why these wiki pages should change",
        "confidence": 0.0,
        "pages": [
            {
                "page_type": "project|concept|decision|person|open_loop|timeline",
                "slug": "kebab-case-slug",
                "title": "Page Title",
                "summary": "source-backed summary",
                "key_points": ["durable point"],
                "sections": {"Required Section": "Markdown body"},
                "related": ["concepts/example"],
                "tags": ["tag"],
                "source_ids": ["document:..."],
                "confidence": 0.0,
            }
        ],
    }
    return (
        "You are the default semantic wiki compiler for a local PKM Brain, following Andrej Karpathy's LLM Wiki pattern.\n"
        "Raw sources are immutable. Your job is to compile them into a persistent, interlinked Markdown wiki that compounds over time.\n"
        "Create or update only source-backed semantic pages. Do not dump raw logs. Do not invent facts beyond the provided sources.\n"
        "Prefer updating existing pages over creating duplicates. Use wikilink targets without .md, such as concepts/wiki-synthesis-layer.\n"
        "The system renders Markdown, frontmatter, index.md, and log.md; you return structured page specs only.\n"
        f"Allowed page types and required type sections: {json.dumps(TYPE_SECTIONS, sort_keys=True)}.\n"
        f"Return JSON exactly matching this shape: {json.dumps(schema, sort_keys=True)}.\n\n"
        f"Existing semantic pages:\n{json.dumps(existing_pages, indent=2)}\n\n"
        f"New or recent source documents:\n{json.dumps(prompt_sources, indent=2)}"
    )


def normalize_page_spec(raw_page: Any) -> dict[str, Any]:
    if not isinstance(raw_page, dict):
        raise ValueError("page spec must be an object")
    page_type = str(raw_page.get("page_type") or "concept").strip()
    if page_type not in SEMANTIC_PAGE_TYPES:
        raise ValueError(f"invalid semantic page_type: {page_type}")
    title = str(raw_page.get("title") or "").strip()
    if not title:
        raise ValueError("semantic page requires title")
    slug = slugify(str(raw_page.get("slug") or title))
    source_ids = [str(item) for item in raw_page.get("source_ids") or [] if str(item).startswith("document:")]
    if not source_ids:
        raise ValueError(f"{title}: semantic page requires document source_ids")
    confidence = max(0.0, min(float(raw_page.get("confidence", 0.7)), 1.0))
    return {
        "page_type": page_type,
        "slug": slug,
        "title": title,
        "summary": str(raw_page.get("summary") or "").strip() or title,
        "key_points": normalize_string_list(raw_page.get("key_points")),
        "sections": normalize_sections(raw_page.get("sections")),
        "related": normalize_related(raw_page.get("related")),
        "tags": normalize_string_list(raw_page.get("tags")),
        "source_ids": source_ids,
        "confidence": confidence,
    }


def normalize_string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def normalize_sections(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {str(key).strip(): str(section).strip() for key, section in value.items() if str(key).strip() and str(section).strip()}


def normalize_related(value: Any) -> list[str]:
    related = []
    for item in normalize_string_list(value):
        item = item.removesuffix(".md").strip("/")
        if item and not Path(item).is_absolute() and ".." not in Path(item).parts:
            related.append(item)
    return related


def page_evidence(spec: dict[str, Any], document_by_source_id: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    evidence = []
    for source_id in spec["source_ids"]:
        document = document_by_source_id.get(source_id)
        if document:
            evidence.append(document)
    return evidence


def page_proposal_change(
    path: Path,
    wiki_root: Path,
    action: str,
    markdown: str,
    spec: dict[str, Any],
    evidence: list[dict[str, Any]],
) -> dict[str, Any]:
    target_path = str(path.relative_to(wiki_root))
    return {
        "target_path": target_path,
        "operation": "create_page" if action == "create" else "replace_page",
        "section_name": None,
        "proposed_markdown": markdown,
        "rationale": f"LLM semantic wiki compile for {spec['title']}.",
        "source_ids": [f"document:{document['id']}" for document in evidence],
        "confidence": float(spec.get("confidence", 0.7)),
    }
