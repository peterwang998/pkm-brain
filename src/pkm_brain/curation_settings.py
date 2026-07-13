from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from .cos_policy import (
    CURATION_STRICTNESS_PROFILES,
    active_policy_version,
    normalize_curation_strictness,
    promote_policy_for_autonomy,
)
from .db import connection
from .paths import BrainPaths
from .util import new_id, now_iso


DEFAULT_CURATION_STRICTNESS = "balanced"
DEFAULT_MERGE_AGGRESSIVENESS = 0.5
DEFAULT_SPLIT_AGGRESSIVENESS = 0.5
DEFAULT_TOPOLOGY_REVIEW_THRESHOLD = 8
MIN_TOPOLOGY_REVIEW_THRESHOLD = 4
MAX_TOPOLOGY_REVIEW_THRESHOLD = 200


def load_curation_settings(paths: BrainPaths) -> dict[str, Any]:
    config = load_local_config(paths.config_file)
    raw = config.get("curation") if isinstance(config.get("curation"), dict) else {}
    strictness = normalize_curation_strictness(
        str(raw.get("strictness") or DEFAULT_CURATION_STRICTNESS)
    )
    merge_aggressiveness = normalize_topology_aggressiveness(
        raw.get("merge_aggressiveness", DEFAULT_MERGE_AGGRESSIVENESS),
        "merge_aggressiveness",
    )
    split_aggressiveness = normalize_topology_aggressiveness(
        raw.get("split_aggressiveness", DEFAULT_SPLIT_AGGRESSIVENESS),
        "split_aggressiveness",
    )
    topology_review_threshold = normalize_topology_review_threshold(
        raw.get("topology_review_threshold", DEFAULT_TOPOLOGY_REVIEW_THRESHOLD)
    )
    with connection(paths.sqlite_path) as conn:
        policy_version = active_policy_version(conn)
    return curation_settings_payload(
        strictness,
        merge_aggressiveness=merge_aggressiveness,
        split_aggressiveness=split_aggressiveness,
        topology_review_threshold=topology_review_threshold,
        policy_version=policy_version,
        updated_at=curation_updated_at(paths.config_file, raw),
        configured=bool(raw.get("strictness")),
        changed=False,
    )


def update_curation_settings(
    paths: BrainPaths,
    strictness: str | None = None,
    *,
    merge_aggressiveness: Any = None,
    split_aggressiveness: Any = None,
    topology_review_threshold: Any = None,
) -> dict[str, Any]:
    if all(
        value is None
        for value in (
            strictness,
            merge_aggressiveness,
            split_aggressiveness,
            topology_review_threshold,
        )
    ):
        raise ValueError("at least one curation setting is required")
    config = load_local_config(paths.config_file)
    curation = (
        config.get("curation") if isinstance(config.get("curation"), dict) else {}
    )
    currently_configured = bool(curation)
    current_strictness = normalize_curation_strictness(
        str(curation.get("strictness") or DEFAULT_CURATION_STRICTNESS)
    )
    current_merge = normalize_topology_aggressiveness(
        curation.get("merge_aggressiveness", DEFAULT_MERGE_AGGRESSIVENESS),
        "merge_aggressiveness",
    )
    current_split = normalize_topology_aggressiveness(
        curation.get("split_aggressiveness", DEFAULT_SPLIT_AGGRESSIVENESS),
        "split_aggressiveness",
    )
    current_topology_threshold = normalize_topology_review_threshold(
        curation.get("topology_review_threshold", DEFAULT_TOPOLOGY_REVIEW_THRESHOLD)
    )
    normalized = normalize_curation_strictness(strictness or current_strictness)
    normalized_merge = normalize_topology_aggressiveness(
        current_merge if merge_aggressiveness is None else merge_aggressiveness,
        "merge_aggressiveness",
    )
    normalized_split = normalize_topology_aggressiveness(
        current_split if split_aggressiveness is None else split_aggressiveness,
        "split_aggressiveness",
    )
    normalized_topology_threshold = normalize_topology_review_threshold(
        current_topology_threshold
        if topology_review_threshold is None
        else topology_review_threshold
    )
    strictness_changed = current_strictness != normalized
    merge_changed = current_merge != normalized_merge
    split_changed = current_split != normalized_split
    topology_threshold_changed = (
        current_topology_threshold != normalized_topology_threshold
    )
    if currently_configured and not any(
        (strictness_changed, merge_changed, split_changed, topology_threshold_changed)
    ):
        return load_curation_settings(paths)

    with connection(paths.sqlite_path) as conn:
        if (
            strictness_changed
            or topology_threshold_changed
            or not curation.get("strictness")
        ):
            profile = CURATION_STRICTNESS_PROFILES[normalized]
            minimum_auto_confidence = float(profile["minimum_auto_confidence"])
            policy_version = promote_policy_for_autonomy(
                conn,
                reason="curation autonomy or topology review threshold changed in Settings",
                large_topology_fact_threshold=normalized_topology_threshold,
                strictness=normalized,
                minimum_auto_confidence=minimum_auto_confidence,
            )
        else:
            policy_version = active_policy_version(conn)
            minimum_auto_confidence = float(
                CURATION_STRICTNESS_PROFILES[normalized]["minimum_auto_confidence"]
            )

    updated_curation = dict(curation)
    updated_curation["strictness"] = normalized
    updated_curation["minimum_auto_confidence"] = minimum_auto_confidence
    updated_curation["merge_aggressiveness"] = normalized_merge
    updated_curation["split_aggressiveness"] = normalized_split
    updated_curation["topology_review_threshold"] = normalized_topology_threshold
    updated_curation["updated_at"] = now_iso()
    config["curation"] = updated_curation
    write_local_config(paths.config_file, config)
    return curation_settings_payload(
        normalized,
        merge_aggressiveness=normalized_merge,
        split_aggressiveness=normalized_split,
        topology_review_threshold=normalized_topology_threshold,
        policy_version=policy_version,
        updated_at=str(updated_curation["updated_at"]),
        configured=True,
        changed=True,
    )


def curation_settings_payload(
    strictness: str,
    *,
    merge_aggressiveness: float,
    split_aggressiveness: float,
    topology_review_threshold: int,
    policy_version: int | None,
    updated_at: str | None,
    configured: bool,
    changed: bool,
) -> dict[str, Any]:
    profile = CURATION_STRICTNESS_PROFILES[strictness]
    return {
        "strictness": strictness,
        "label": profile["label"],
        "minimum_auto_confidence": profile["minimum_auto_confidence"],
        "merge_aggressiveness": merge_aggressiveness,
        "split_aggressiveness": split_aggressiveness,
        "topology_review_threshold": topology_review_threshold,
        "policy_version": policy_version,
        "updated_at": updated_at,
        "configured": configured,
        "changed": changed,
        "applies_to": "future_actions_only",
        "existing_queue_unchanged": True,
        "topology_applies_to": "future_gardener_runs_only",
        "profiles": [
            {
                "id": profile_id,
                "label": values["label"],
                "minimum_auto_confidence": values["minimum_auto_confidence"],
            }
            for profile_id, values in CURATION_STRICTNESS_PROFILES.items()
        ],
        "hard_review_boundaries": [
            "truth_contradiction",
            "missing_quote_or_fallback_route",
            "cross_type_topology",
            "unconfirmed_topology_above_review_threshold",
            "failed_topology_judgment",
            "failed_eval_gate",
        ],
    }


def curation_updated_at(path: Path, raw: dict[str, Any]) -> str | None:
    configured = str(raw.get("updated_at") or "").strip()
    if configured:
        return configured
    if not path.exists():
        return None
    return (
        datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        .replace(microsecond=0)
        .isoformat()
    )


def normalize_topology_aggressiveness(value: Any, field: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a number between 0 and 1") from exc
    if not 0.0 <= parsed <= 1.0:
        raise ValueError(f"{field} must be between 0 and 1")
    return round(parsed, 2)


def normalize_topology_review_threshold(value: Any) -> int:
    try:
        numeric = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("topology_review_threshold must be an integer") from exc
    if not numeric.is_integer():
        raise ValueError("topology_review_threshold must be an integer")
    parsed = int(numeric)
    if not MIN_TOPOLOGY_REVIEW_THRESHOLD <= parsed <= MAX_TOPOLOGY_REVIEW_THRESHOLD:
        raise ValueError(
            "topology_review_threshold must be between "
            f"{MIN_TOPOLOGY_REVIEW_THRESHOLD} and {MAX_TOPOLOGY_REVIEW_THRESHOLD}"
        )
    return parsed


def load_local_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(loaded, dict):
        raise ValueError(f"{path} must contain a YAML mapping")
    return loaded


def write_local_config(path: Path, config: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{new_id('tmp')}")
    try:
        temporary.write_text(
            yaml.safe_dump(config, sort_keys=False, allow_unicode=False),
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
