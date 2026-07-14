"""Shared model defaults for bounded Chief-of-Staff inference stages.

Operational selection, evidence validation, ranking, and lifecycle transitions stay
deterministic. These defaults apply only where a Chief-of-Staff stage explicitly
uses the restricted Codex inference pipeline.
"""

from __future__ import annotations


DEFAULT_COS_CODEX_MODEL = "gpt-5.6-luna"
DEFAULT_COS_CODEX_REASONING_EFFORT = "high"

COS_CODEX_MODEL_ENV = "PKM_BRAIN_COS_CODEX_MODEL"
COS_CODEX_REASONING_EFFORT_ENV = "PKM_BRAIN_COS_CODEX_REASONING_EFFORT"
