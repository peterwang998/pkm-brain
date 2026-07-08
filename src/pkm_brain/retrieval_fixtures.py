from __future__ import annotations

import json
from importlib.resources import files
from pathlib import Path
from typing import Any

import yaml


BUILTIN_RETRIEVAL_GOLDENS_RESOURCE = "resources/retrieval_golden_cases.json"


def load_builtin_retrieval_golden_cases() -> list[dict[str, Any]]:
    raw = files("pkm_brain").joinpath(BUILTIN_RETRIEVAL_GOLDENS_RESOURCE).read_text(encoding="utf-8")
    return normalize_retrieval_golden_cases(
        json.loads(raw),
        origin="built_in",
        source=f"package:{BUILTIN_RETRIEVAL_GOLDENS_RESOURCE}",
    )


def load_local_retrieval_golden_cases(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    if isinstance(loaded, dict):
        loaded = loaded.get("cases") or []
    if not isinstance(loaded, list):
        raise ValueError(f"{path} must contain a list of retrieval eval cases or a mapping with a 'cases' list")
    return normalize_retrieval_golden_cases(loaded, origin="local", source=str(path))


def load_retrieval_golden_cases(paths: Any) -> list[dict[str, Any]]:
    return [
        *RETRIEVAL_GOLDEN_CASES,
        *load_local_retrieval_golden_cases(paths.golden_queries_file),
    ]


def normalize_retrieval_golden_cases(
    cases: list[Any],
    *,
    origin: str,
    source: str,
) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for index, raw_case in enumerate(cases, start=1):
        if not isinstance(raw_case, dict):
            raise ValueError(f"{source}: case {index} must be a mapping")
        case = dict(raw_case)
        case.setdefault("id", f"{origin}-{index:03d}")
        if not str(case.get("query") or "").strip():
            raise ValueError(f"{source}: case {case['id']} is missing query")
        case["query"] = str(case["query"])
        case["kind"] = str(case.get("kind") or "local_query")
        case["expected_verdict"] = str(case.get("expected_verdict") or "found")
        case["expected_source_ids"] = normalize_expected_sources(case)
        if "expected_vector_sources" in case and "expected_vector_source_ids" not in case:
            case["expected_vector_source_ids"] = case["expected_vector_sources"]
        case["expected_vector_source_ids"] = sorted(
            str(item) for item in (case.get("expected_vector_source_ids") or [])
        )
        case["origin"] = origin
        case["fixture_source"] = source
        normalized.append(case)
    return normalized


def normalize_expected_sources(case: dict[str, Any]) -> list[str]:
    expected = case.get("expected_source_ids")
    if expected is None:
        expected = case.get("expected_sources")
    return sorted(str(item) for item in (expected or []))


RETRIEVAL_GOLDEN_CASES: list[dict[str, Any]] = load_builtin_retrieval_golden_cases()
