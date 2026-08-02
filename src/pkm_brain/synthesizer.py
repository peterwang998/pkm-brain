from __future__ import annotations

from typing import Any

from .cos_actions import propose_action
from .llm import LLMProvider, complete_json, cos_role_provider_configured, get_cos_role_provider
from .paths import BrainPaths
from .wiki_facts import (
    active_facts_by_page,
    active_page_synthesis,
    fact_set_hash,
    managed_fact_page_summaries,
)


SYNTHESIZER_PROMPT_VERSION = "synthesizer-page-v1"
SYNTHESIS_SCHEMA = {
    "type": "object",
    "required": ["syntheses"],
    "properties": {
        "syntheses": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["page_hint", "synthesis_markdown", "fact_ids"],
            },
        }
    },
}


def generate_page_syntheses(
    paths: BrainPaths,
    *,
    shadow: bool = True,
    max_pages: int = 10,
    run_id: str | None = None,
    llm_provider: LLMProvider | None = None,
    provider: str | None = None,
) -> dict[str, Any]:
    page_hints = candidate_synthesis_page_hints(paths, max_pages=max_pages)
    if not page_hints:
        return {
            "status": "ok",
            "shadow": shadow,
            "page_count": 0,
            "candidates": [],
            "actions": [],
        }
    if not cos_role_provider_configured(paths, "synthesizer", llm_provider=llm_provider, provider=provider):
        return {
            "status": "skipped",
            "reason": "No CoS LLM provider configured for synthesizer role",
            "shadow": shadow,
            "page_count": len(page_hints),
            "candidates": [],
            "actions": [],
        }

    facts_by_page = active_facts_by_page(paths, page_hints)
    page_cards = [
        synthesis_page_card(page_hint, facts_by_page.get(page_hint) or [])
        for page_hint in page_hints
        if facts_by_page.get(page_hint)
    ]
    active_provider = get_cos_role_provider(paths, "synthesizer", provider=provider, llm_provider=llm_provider)
    parsed = complete_json(
        synthesis_prompt(page_cards),
        schema=SYNTHESIS_SCHEMA,
        role="synthesizer",
        provider=provider,
        llm_provider=active_provider,
        paths=paths,
    )
    candidates = validate_synthesis_candidates(
        parsed.get("syntheses") or [],
        facts_by_page=facts_by_page,
        model=getattr(active_provider, "model", None),
    )
    actions: list[dict[str, Any]] = []
    if not shadow:
        for candidate in candidates:
            actions.append(
                propose_action(
                    paths,
                    "synthesize_page",
                    run_id=run_id,
                    action_payload={"synthesis": candidate},
                    action_features={
                        "candidate_signal": "page_synthesis",
                        "affected_fact_count": len(candidate.get("fact_ids") or []),
                        "truth_mutation": False,
                        "reversible": True,
                    },
                    target_fact_ids=list(candidate.get("fact_ids") or []),
                    target_page_paths=[str(candidate["page_hint"])],
                    proposed_by="synthesizer",
                    risk_tier="low",
                    decide=True,
                )
            )
    return {
        "status": "ok",
        "shadow": shadow,
        "page_count": len(page_hints),
        "candidate_count": len(candidates),
        "candidates": candidates,
        "actions": actions,
    }


def candidate_synthesis_page_hints(paths: BrainPaths, *, max_pages: int) -> list[str]:
    summaries = managed_fact_page_summaries(paths)
    candidates: list[tuple[str, str]] = []
    for summary in summaries:
        page_hint = str(summary.get("relative_path") or "")
        if not page_hint or int(summary.get("active_fact_count") or 0) <= 0:
            continue
        facts = active_facts_by_page(paths, [page_hint]).get(page_hint, [])
        if not facts:
            continue
        synthesis = active_page_synthesis(paths, page_hint, facts)
        if synthesis and not synthesis.get("stale_by_hash"):
            continue
        latest = str(summary.get("latest_observed_at") or "")
        candidates.append((latest, page_hint))
    candidates.sort(reverse=True)
    return [page_hint for _latest, page_hint in candidates[:max_pages]]


def synthesis_page_card(page_hint: str, facts: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "page_hint": page_hint,
        "fact_hash": fact_set_hash(facts),
        "facts": [
            {
                "id": fact.get("id"),
                "statement": fact.get("statement"),
                "section_hint": fact.get("section_hint"),
                "source_ids": fact.get("source_ids") or [],
                "truth_confidence": fact.get("truth_confidence", fact.get("confidence")),
            }
            for fact in facts
        ],
    }


def synthesis_prompt(page_cards: list[dict[str, Any]]) -> str:
    return (
        "Write concise non-canonical wiki synthesis blocks from active facts only. "
        "Use Markdown bullets. Every bullet must cite at least one fact id in square brackets, "
        "for example [fact_abc]. Do not introduce claims that are not directly supported by the facts. "
        "Return syntheses with page_hint, synthesis_markdown, and fact_ids.\n\n"
        f"Pages:\n{page_cards}"
    )


def validate_synthesis_candidates(
    raw_syntheses: list[Any],
    *,
    facts_by_page: dict[str, list[dict[str, Any]]],
    model: str | None,
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for item in raw_syntheses:
        if not isinstance(item, dict):
            continue
        page_hint = str(item.get("page_hint") or "")
        facts = facts_by_page.get(page_hint) or []
        fact_ids = [str(fact["id"]) for fact in facts]
        allowed_fact_ids = set(fact_ids)
        selected_fact_ids = [
            str(fact_id)
            for fact_id in item.get("fact_ids") or []
            if str(fact_id) in allowed_fact_ids
        ]
        markdown = str(item.get("synthesis_markdown") or "").strip()
        if not page_hint or not facts or not markdown or not selected_fact_ids:
            continue
        cited_fact_ids = [fact_id for fact_id in selected_fact_ids if f"[{fact_id}]" in markdown]
        if not cited_fact_ids:
            continue
        candidates.append(
            {
                "page_hint": page_hint,
                "synthesis_markdown": markdown,
                "fact_ids": cited_fact_ids,
                "fact_hash": fact_set_hash(facts),
                "model": model,
                "prompt_version": SYNTHESIZER_PROMPT_VERSION,
            }
        )
    return candidates
