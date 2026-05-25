from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .db import connection, dumps, loads, rows
from .llm import get_provider
from .paths import BrainPaths
from .title_utils import bounded_document_title
from .util import new_id, now_iso
from .wiki import lint_wiki


VALID_BATCH_STATUSES = {"proposed", "needs_interview", "approved", "rejected", "applied", "superseded", "failed"}
VALID_ITEM_OPERATIONS = {"replace_section", "append_section", "create_page", "replace_page"}


@dataclass(frozen=True)
class WikiChange:
    target_path: str
    operation: str
    proposed_markdown: str
    rationale: str
    section_name: str | None = None
    source_ids: list[str] | None = None
    confidence: float = 0.8


def create_wiki_proposal(
    paths: BrainPaths,
    title: str,
    rationale: str,
    source_ids: list[str],
    changes: list[dict[str, Any] | WikiChange],
    confidence: float,
    author: str = "agent",
    source: str = "mcp",
    status: str = "proposed",
) -> str:
    if status not in VALID_BATCH_STATUSES:
        raise ValueError(f"invalid proposal status: {status}")
    normalized = [normalize_change(change) for change in changes]
    if not normalized:
        raise ValueError("proposal requires at least one change item")
    timestamp = now_iso()
    batch_id = new_id("wikibatch")
    with connection(paths.sqlite_path) as conn:
        conn.execute(
            """
            INSERT INTO wiki_change_batches(
              id, title, rationale, author, source, status, confidence, source_ids, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (batch_id, title, rationale, author, source, status, confidence, dumps(source_ids), timestamp),
        )
        for index, change in enumerate(normalized):
            validate_target_path(change.target_path)
            if change.operation not in VALID_ITEM_OPERATIONS:
                raise ValueError(f"invalid item operation: {change.operation}")
            conn.execute(
                """
                INSERT INTO wiki_change_items(
                  id, batch_id, order_index, target_path, operation, section_name,
                  proposed_markdown, rationale, source_ids, confidence
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    new_id("wikiitem"),
                    batch_id,
                    index,
                    change.target_path,
                    change.operation,
                    change.section_name,
                    change.proposed_markdown,
                    change.rationale,
                    dumps(change.source_ids or source_ids),
                    change.confidence,
                ),
            )
    return batch_id


def normalize_change(change: dict[str, Any] | WikiChange) -> WikiChange:
    if isinstance(change, WikiChange):
        return change
    return WikiChange(
        target_path=str(change["target_path"]),
        operation=str(change.get("operation") or "replace_section"),
        section_name=change.get("section_name"),
        proposed_markdown=str(change["proposed_markdown"]),
        rationale=str(change.get("rationale") or ""),
        source_ids=list(change.get("source_ids") or []),
        confidence=float(change.get("confidence", 0.8)),
    )


def validate_target_path(target_path: str) -> None:
    path = Path(target_path)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"target_path must be relative to wiki root: {target_path}")
    if path.suffix != ".md":
        raise ValueError(f"target_path must point to a Markdown file: {target_path}")


def list_wiki_proposals(paths: BrainPaths, status: str | None = None) -> list[dict[str, Any]]:
    query = "SELECT * FROM wiki_change_batches"
    params: list[str] = []
    if status:
        query += " WHERE status = ?"
        params.append(status)
    query += " ORDER BY created_at DESC"
    with connection(paths.sqlite_path) as conn:
        found = [row_to_batch(row) for row in conn.execute(query, params)]
    return found


def inspect_wiki_proposal(paths: BrainPaths, batch_id: str) -> dict[str, Any]:
    with connection(paths.sqlite_path) as conn:
        batch = conn.execute("SELECT * FROM wiki_change_batches WHERE id = ?", (batch_id,)).fetchone()
        if not batch:
            raise ValueError(f"wiki proposal not found: {batch_id}")
        items = rows(conn, "SELECT * FROM wiki_change_items WHERE batch_id = ? ORDER BY order_index", (batch_id,))
        interviews = rows(conn, "SELECT * FROM wiki_interviews WHERE batch_id = ? ORDER BY created_at", (batch_id,))
    output = row_to_batch(batch)
    output["items"] = [row_to_item(item) for item in items]
    output["interviews"] = [row_to_interview(interview) for interview in interviews]
    return output


def reject_wiki_proposal(paths: BrainPaths, batch_id: str, reason: str | None = None) -> dict[str, Any]:
    timestamp = now_iso()
    with connection(paths.sqlite_path) as conn:
        conn.execute(
            """
            UPDATE wiki_change_batches
            SET status = 'rejected', reviewed_at = ?, error = ?
            WHERE id = ?
            """,
            (timestamp, reason, batch_id),
        )
    return inspect_wiki_proposal(paths, batch_id)


def record_wiki_interview(
    paths: BrainPaths,
    batch_id: str,
    questions: list[str],
    answers: list[str],
    disposition: str,
    provider: str | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    if disposition not in {"approved", "rejected", "needs_interview"}:
        raise ValueError("disposition must be approved, rejected, or needs_interview")
    timestamp = now_iso()
    with connection(paths.sqlite_path) as conn:
        conn.execute(
            """
            INSERT INTO wiki_interviews(id, batch_id, questions, answers, disposition, provider, model, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (new_id("wikiinterview"), batch_id, dumps(questions), dumps(answers), disposition, provider, model, timestamp),
        )
        conn.execute(
            """
            UPDATE wiki_change_batches
            SET status = ?, reviewed_at = ?
            WHERE id = ?
            """,
            (disposition, timestamp, batch_id),
        )
    return inspect_wiki_proposal(paths, batch_id)


def default_interview_questions(proposal: dict[str, Any]) -> list[str]:
    targets = ", ".join(sorted({item["target_path"] for item in proposal.get("items", [])}))
    return [
        f"Are the proposed changes to {targets} accurate and worth making durable?",
        "Is any important nuance, caveat, or source missing?",
        "Should this be approved now, rejected, or kept for another interview?",
    ]


def generate_interview_questions(paths: BrainPaths, batch_id: str, provider_name: str | None = None) -> dict[str, Any]:
    proposal = inspect_wiki_proposal(paths, batch_id)
    try:
        provider = get_provider(provider_name)
    except Exception:
        return {"questions": default_interview_questions(proposal), "provider": None, "model": None}
    prompt = interview_prompt(proposal)
    parsed = parse_json_object(provider.complete(prompt))
    questions = [str(item) for item in parsed.get("questions", []) if str(item).strip()]
    return {
        "questions": questions or default_interview_questions(proposal),
        "provider": provider.name,
        "model": provider.model,
    }


def apply_wiki_proposal(paths: BrainPaths, batch_id: str) -> dict[str, Any]:
    proposal = inspect_wiki_proposal(paths, batch_id)
    if proposal["status"] != "approved":
        raise ValueError(f"wiki proposal must be approved before apply; current status is {proposal['status']}")
    changed_paths: list[str] = []
    originals: dict[Path, str | None] = {}
    for item in proposal["items"]:
        target = paths.wiki / item["target_path"]
        if target not in originals:
            originals[target] = target.read_text(encoding="utf-8") if target.exists() else None
        target.parent.mkdir(parents=True, exist_ok=True)
        if item["operation"] == "create_page":
            if target.exists():
                raise ValueError(f"target already exists for create_page: {item['target_path']}")
            target.write_text(item["proposed_markdown"].rstrip() + "\n", encoding="utf-8")
        elif item["operation"] == "replace_page":
            if not target.exists():
                raise ValueError(f"target does not exist: {item['target_path']}")
            target.write_text(item["proposed_markdown"].rstrip() + "\n", encoding="utf-8")
        else:
            if not target.exists():
                raise ValueError(f"target does not exist: {item['target_path']}")
            text = target.read_text(encoding="utf-8")
            if item["operation"] == "replace_section":
                text = replace_section(text, item["section_name"] or "", item["proposed_markdown"])
            elif item["operation"] == "append_section":
                text = append_to_section(text, item["section_name"] or "", item["proposed_markdown"])
            else:
                raise ValueError(f"unsupported operation: {item['operation']}")
            target.write_text(text, encoding="utf-8")
        changed_paths.append(str(target))
    lint_result = lint_wiki(paths)
    if lint_result["errors"]:
        restore_originals(originals)
    timestamp = now_iso()
    with connection(paths.sqlite_path) as conn:
        conn.execute(
            """
            UPDATE wiki_change_batches
            SET status = ?, applied_at = ?, error = ?
            WHERE id = ?
            """,
            ("failed" if lint_result["errors"] else "applied", timestamp, "; ".join(lint_result["errors"]) or None, batch_id),
        )
    return {"batch_id": batch_id, "changed_paths": changed_paths, "lint": lint_result, "proposal": inspect_wiki_proposal(paths, batch_id)}


def restore_originals(originals: dict[Path, str | None]) -> None:
    for path, content in originals.items():
        if content is None:
            if path.exists():
                path.unlink()
        else:
            path.write_text(content, encoding="utf-8")


def replace_section(markdown: str, section_name: str, replacement: str) -> str:
    if not section_name:
        raise ValueError("replace_section requires section_name")
    pattern = re.compile(rf"(^##\s+{re.escape(section_name)}\s*\n)(.*?)(?=^##\s+|\Z)", flags=re.MULTILINE | re.DOTALL)
    if pattern.search(markdown):
        return pattern.sub(lambda match: f"{match.group(1)}\n{replacement.strip()}\n\n", markdown, count=1)
    return markdown.rstrip() + f"\n\n## {section_name}\n\n{replacement.strip()}\n"


def append_to_section(markdown: str, section_name: str, addition: str) -> str:
    if not section_name:
        raise ValueError("append_section requires section_name")
    pattern = re.compile(rf"(^##\s+{re.escape(section_name)}\s*\n)(.*?)(?=^##\s+|\Z)", flags=re.MULTILINE | re.DOTALL)
    match = pattern.search(markdown)
    if not match:
        return markdown.rstrip() + f"\n\n## {section_name}\n\n{addition.strip()}\n"
    existing = match.group(2).rstrip()
    body = f"{existing}\n\n{addition.strip()}\n\n"
    return markdown[: match.start(2)] + body + markdown[match.end(2) :]


def propose_from_sources(paths: BrainPaths, provider_name: str | None = None, limit: int = 8) -> dict[str, Any]:
    provider = get_provider(provider_name)
    documents = latest_documents(paths, limit)
    if not documents:
        return {"created": False, "reason": "no source documents found"}
    prompt = proposal_prompt(documents)
    parsed = parse_json_object(provider.complete(prompt))
    changes = parsed.get("changes") or []
    if not changes:
        return {"created": False, "reason": "provider returned no changes", "provider": provider.name, "model": provider.model}
    batch_id = create_wiki_proposal(
        paths,
        title=str(parsed.get("title") or "LLM wiki proposal"),
        rationale=str(parsed.get("rationale") or "LLM-generated proposal from recent sources."),
        source_ids=list(parsed.get("source_ids") or [doc["source_id"] for doc in documents]),
        changes=changes,
        confidence=float(parsed.get("confidence", 0.7)),
        author=f"llm:{provider.name}",
        source="nightly",
        status="needs_interview",
    )
    return {"created": True, "batch_id": batch_id, "provider": provider.name, "model": provider.model}


def latest_documents(paths: BrainPaths, limit: int) -> list[dict[str, Any]]:
    with connection(paths.sqlite_path) as conn:
        found = rows(
            conn,
            """
            SELECT d.id, d.title, d.source_type, d.source_path, d.ingested_at,
                   GROUP_CONCAT(c.text, '\n\n') AS chunk_text
            FROM documents d
            LEFT JOIN chunks c ON c.document_id = d.id
            GROUP BY d.id
            ORDER BY d.ingested_at DESC
            LIMIT ?
            """,
            (limit,),
        )
    output: list[dict[str, Any]] = []
    for row in found:
        text = re.sub(r"\s+", " ", row["chunk_text"] or "").strip()
        output.append(
            {
                "source_id": f"document:{row['id']}",
                "title": bounded_document_title(row["title"], str(row["id"])),
                "source_type": row["source_type"],
                "source_path": row["source_path"],
                "ingested_at": row["ingested_at"],
                "preview": text[:1500],
            }
        )
    return output


def proposal_prompt(documents: list[dict[str, Any]]) -> str:
    return (
        "You maintain a local Markdown personal wiki. Propose source-backed wiki changes as JSON.\n"
        "Only propose durable, human-readable semantic wiki updates. Do not dump raw logs.\n"
        "Return this JSON shape exactly: {\"title\": str, \"rationale\": str, \"confidence\": number, "
        "\"source_ids\": [str], \"changes\": [{\"target_path\": str, \"operation\": \"replace_section\"|\"append_section\"|\"create_page\"|\"replace_page\", "
        "\"section_name\": str|null, \"proposed_markdown\": str, \"rationale\": str, \"source_ids\": [str], \"confidence\": number}]}.\n"
        "Target paths must be relative to wiki root, like concepts/example.md or decisions/example.md.\n\n"
        f"Sources:\n{json.dumps(documents, indent=2)}"
    )


def interview_prompt(proposal: dict[str, Any]) -> str:
    return (
        "Generate 3 concise interview questions for a human reviewer of this wiki proposal. "
        "Focus on correctness, missing nuance, and whether the proposal should be durable. "
        "Return JSON: {\"questions\": [str, str, str]}.\n\n"
        f"Proposal:\n{json.dumps(proposal, indent=2)}"
    )


def parse_json_object(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"\A```(?:json)?\s*", "", stripped)
        stripped = re.sub(r"\s*```\Z", "", stripped)
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", stripped, flags=re.DOTALL)
        if not match:
            raise
        parsed = json.loads(match.group(0))
    if not isinstance(parsed, dict):
        raise ValueError("LLM response must be a JSON object")
    return parsed


def row_to_batch(row: Any) -> dict[str, Any]:
    output = dict(row)
    output["source_ids"] = loads(output.get("source_ids"), [])
    return output


def row_to_item(row: Any) -> dict[str, Any]:
    output = dict(row)
    output["source_ids"] = loads(output.get("source_ids"), [])
    return output


def row_to_interview(row: Any) -> dict[str, Any]:
    output = dict(row)
    output["questions"] = loads(output.get("questions"), [])
    output["answers"] = loads(output.get("answers"), [])
    return output
