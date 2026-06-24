from __future__ import annotations

from typing import Any

from .cos_actions import propose_action
from .db import connection, rows
from .llm import LLMProvider, complete_json
from .paths import BrainPaths
from .util import now_iso
from .wiki_facts import canonical_page_hint_for_fact, entity_key_for_change, topic_for_path


EXTRACTION_SCHEMA = {
    "type": "object",
    "required": ["facts"],
    "properties": {
        "facts": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["statement", "source_spans"],
            },
        }
    },
}


def extract_recent_documents(
    paths: BrainPaths,
    *,
    limit: int = 10,
    shadow: bool = True,
    llm_provider: LLMProvider | None = None,
    provider: str | None = None,
) -> dict[str, Any]:
    documents = recent_source_cards(paths, limit=limit)
    if not documents:
        return {"status": "ok", "shadow": shadow, "documents": [], "candidates": [], "actions": []}
    if llm_provider is None and provider is None:
        return {
            "status": "skipped",
            "reason": "LLM extraction is disabled unless a provider or fake provider is supplied",
            "shadow": shadow,
            "documents": documents,
            "candidates": [],
            "actions": [],
        }
    parsed = complete_json(
        extraction_prompt(documents),
        schema=EXTRACTION_SCHEMA,
        provider=provider,
        role="extractor",
        llm_provider=llm_provider,
    )
    candidates = validate_extracted_facts(paths, parsed.get("facts") or [])
    actions: list[dict[str, Any]] = []
    if not shadow:
        for candidate in candidates:
            actions.append(
                propose_action(
                    paths,
                    "fact_upsert",
                    action_payload={"fact": candidate},
                    action_features={
                        "candidate_signal": "source_extraction",
                        "affected_fact_count": 1,
                        "reversible": True,
                        "truth_mutation": False,
                        "eval_gate": {"suite": "extraction", "passed": False},
                    },
                    target_page_paths=[candidate["page_hint"]],
                    proposed_by="extractor",
                    confidence=candidate.get("truth_confidence"),
                    risk_tier="medium",
                    decide=True,
                )
            )
    return {
        "status": "ok",
        "shadow": shadow,
        "documents": documents,
        "candidates": candidates,
        "actions": actions,
    }


def recent_source_cards(paths: BrainPaths, *, limit: int) -> list[dict[str, Any]]:
    with connection(paths.sqlite_path) as conn:
        doc_rows = rows(
            conn,
            """
            SELECT *
            FROM documents
            WHERE status = 'active'
            ORDER BY ingested_at DESC, id DESC
            LIMIT ?
            """,
            (limit,),
        )
        output: list[dict[str, Any]] = []
        for document in doc_rows:
            chunks = [
                {
                    "chunk_id": row["id"],
                    "heading_path": row["heading_path"],
                    "start_offset": row["start_offset"],
                    "end_offset": row["end_offset"],
                    "text": row["text"][:2000],
                }
                for row in conn.execute(
                    """
                    SELECT *
                    FROM chunks
                    WHERE document_id = ?
                    ORDER BY chunk_index
                    LIMIT 6
                    """,
                    (document["id"],),
                )
            ]
            output.append(
                {
                    "document_id": document["id"],
                    "title": document["title"],
                    "source_type": document["source_type"],
                    "source_id": f"document:{document['id']}",
                    "chunks": chunks,
                }
            )
        return output


def extraction_prompt(documents: list[dict[str, Any]]) -> str:
    return (
        "Extract atomic source-backed facts from these source cards. "
        "Each fact must include statement, source_spans with chunk_id/start/end, "
        "page_hint, section_hint, entity_key if known, and extraction/routing/truth confidences.\n\n"
        f"Source cards:\n{documents}"
    )


def validate_extracted_facts(
    paths: BrainPaths, raw_facts: list[Any]
) -> list[dict[str, Any]]:
    chunk_text_by_id = load_chunk_texts(paths)
    candidates: list[dict[str, Any]] = []
    for item in raw_facts:
        if not isinstance(item, dict):
            continue
        statement = str(item.get("statement") or "").strip()
        spans = [span for span in item.get("source_spans") or [] if isinstance(span, dict)]
        if not statement or not spans:
            continue
        valid_spans = []
        source_ids = []
        for span in spans:
            chunk_id = str(span.get("chunk_id") or "")
            text = chunk_text_by_id.get(chunk_id)
            if text is None:
                continue
            start = int(span.get("start", 0) or 0)
            end = int(span.get("end", len(text)) or len(text))
            if start < 0 or end < start or end > len(text):
                continue
            valid_spans.append({"chunk_id": chunk_id, "start": start, "end": end})
            source_ids.append(f"chunk:{chunk_id}")
        if not valid_spans:
            continue
        page_hint = canonical_page_hint_for_fact(str(item.get("page_hint") or "concepts/extracted-facts.md"))
        section_hint = str(item.get("section_hint") or "Summary")
        entity_key = str(item.get("entity_key") or entity_key_for_change(topic_for_path(page_hint), page_hint, section_hint))
        candidates.append(
            {
                "statement": statement,
                "entity_key": entity_key,
                "page_hint": page_hint,
                "section_hint": section_hint,
                "source_ids": sorted(set(source_ids)),
                "source_spans": valid_spans,
                "evidence_quote": str(item.get("evidence_quote") or "")[:1000] or None,
                "observed_at": item.get("observed_at") or now_iso(),
                "effective_at": item.get("effective_at"),
                "confidence": float(item.get("truth_confidence") or item.get("confidence") or 0.5),
                "extraction_confidence": optional_float(item.get("extraction_confidence")),
                "routing_confidence": optional_float(item.get("routing_confidence")),
                "truth_confidence": optional_float(item.get("truth_confidence")) or float(item.get("confidence") or 0.5),
                "extraction_method": "llm",
                "extractor_model": item.get("extractor_model"),
                "metadata": {"source": "source_to_facts_extraction"},
            }
        )
    return candidates


def load_chunk_texts(paths: BrainPaths) -> dict[str, str]:
    with connection(paths.sqlite_path) as conn:
        return {str(row["id"]): str(row["text"] or "") for row in conn.execute("SELECT id, text FROM chunks")}


def optional_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
