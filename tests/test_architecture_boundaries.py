from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULE_LINE_LIMITS = {
    "src/pkm_brain/ui_server.py": 6032,
    "src/pkm_brain/service.py": 4920,
    "src/pkm_brain/extraction.py": 4402,
    "src/pkm_brain/wiki_facts.py": 3540,
    "src/pkm_brain/cos_actions.py": 3353,
    "app/Sources/Views/Queue/QueueView.swift": 1952,
}


def test_large_modules_do_not_grow() -> None:
    growth = {}
    for relative_path, limit in MODULE_LINE_LIMITS.items():
        line_count = len((ROOT / relative_path).read_text(encoding="utf-8").splitlines())
        if line_count > limit:
            growth[relative_path] = {"limit": limit, "actual": line_count}

    assert not growth, (
        "Large-module ratchet exceeded. Put new behavior in a focused module or reduce "
        f"the guarded file first: {growth}"
    )
