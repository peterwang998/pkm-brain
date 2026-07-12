from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_brain_memory_skill_copies_match_except_packaging_terms() -> None:
    codex = (ROOT / "skills/brain-memory/SKILL.md").read_text(encoding="utf-8")
    claude = (
        ROOT
        / "claude-marketplace/plugins/pkm-brain-memory/skills/brain-memory/SKILL.md"
    ).read_text(encoding="utf-8")

    assert normalize_brain_memory_skill(codex) == normalize_brain_memory_skill(claude)


def normalize_brain_memory_skill(text: str) -> str:
    lines = []
    for line in text.splitlines():
        if line.startswith("version:"):
            continue
        lines.append(
            line.replace("This skill is an activation policy.", "The skill is an activation policy.")
            .replace("Claude Code structured tool results", "Codex structured tool results")
        )
    return "\n".join(lines).strip()
