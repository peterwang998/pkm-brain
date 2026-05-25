from __future__ import annotations

import re

MAX_DOCUMENT_TITLE_CHARS = 500
TITLE_TRUNCATION_SUFFIX = "... [truncated]"
CODEX_PROVIDER_PROMPT_PREFIX = "You are running as the Codex-backed LLM provider for pkm-brain."


def compact_title_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def bounded_document_title(value: str | None, fallback: str, max_chars: int = MAX_DOCUMENT_TITLE_CHARS) -> str:
    title = compact_title_text(value) or compact_title_text(fallback) or "Untitled"
    if len(title) <= max_chars:
        return title
    keep_chars = max_chars - len(TITLE_TRUNCATION_SUFFIX)
    if keep_chars <= 0:
        return title[:max_chars]
    return f"{title[:keep_chars].rstrip()}{TITLE_TRUNCATION_SUFFIX}"


def is_codex_provider_prompt(value: str | None) -> bool:
    return str(value or "").lstrip().startswith(CODEX_PROVIDER_PROMPT_PREFIX)


def is_self_generated_codex_provider_session(
    agent: str | None,
    title: str | None,
    user_messages: list[str] | None = None,
) -> bool:
    if str(agent or "").lower() != "codex":
        return False
    if is_codex_provider_prompt(title):
        return True
    return any(is_codex_provider_prompt(message) for message in user_messages or [])
