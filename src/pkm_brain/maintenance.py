from __future__ import annotations

import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .indexes import path_size
from .paths import BrainPaths
from .util import now_iso


def prune_runtime_artifacts(
    paths: BrainPaths,
    *,
    commit: bool = False,
    keep_runtime_backups: int = 3,
    keep_days: int = 30,
    max_log_bytes: int = 10_000_000,
    keep_log_rotations: int = 3,
) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=keep_days)
    actions: list[dict[str, Any]] = []
    manual_review: list[dict[str, Any]] = []
    actions.extend(stale_db_backup_actions(paths))
    actions.extend(runtime_backup_actions(paths, cutoff=cutoff, keep=keep_runtime_backups))
    actions.extend(log_rotation_actions(paths, max_log_bytes=max_log_bytes))
    manual_review.extend(experiment_home_reports(paths))
    if commit:
        for action in actions:
            apply_prune_action(action, keep_log_rotations=keep_log_rotations)
    return {
        "status": "ok" if commit else "dry_run",
        "dry_run": not commit,
        "generated_at": now_iso(),
        "policy": {
            "keep_runtime_backups": keep_runtime_backups,
            "keep_days": keep_days,
            "max_log_bytes": max_log_bytes,
            "keep_log_rotations": keep_log_rotations,
        },
        "action_count": len(actions),
        "total_bytes_reclaimable": sum(int(action.get("bytes") or 0) for action in actions),
        "actions": actions,
        "manual_review": manual_review,
    }


def stale_db_backup_actions(paths: BrainPaths) -> list[dict[str, Any]]:
    if not paths.db_dir.exists():
        return []
    return [
        action("delete_file", path, reason="stale db/*.bak.gz backup")
        for path in sorted(paths.db_dir.glob("*.bak.gz"))
    ]


def runtime_backup_actions(paths: BrainPaths, *, cutoff: datetime, keep: int) -> list[dict[str, Any]]:
    candidates: list[Path] = []
    for root in runtime_backup_roots(paths):
        if root.exists():
            candidates.extend(path for path in root.iterdir() if path.is_dir())
    ordered = sorted(candidates, key=lambda path: path.stat().st_mtime, reverse=True)
    stale = []
    for index, path in enumerate(ordered):
        modified = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
        if index >= keep and modified < cutoff:
            stale.append(action("delete_tree", path, reason="runtime backup outside retention window"))
    return stale


def log_rotation_actions(paths: BrainPaths, *, max_log_bytes: int) -> list[dict[str, Any]]:
    if not paths.logs.exists():
        return []
    return [
        action("rotate_log", path, reason=f"log exceeds {max_log_bytes} bytes")
        for path in sorted(paths.logs.glob("*.log"))
        if path.is_file() and path.stat().st_size > max_log_bytes
    ]


def experiment_home_reports(paths: BrainPaths) -> list[dict[str, Any]]:
    reports = []
    for root in [paths.home.parent / "brain-shadow", paths.home.parent / "brain-forks"]:
        if root.exists():
            reports.append(
                {
                    "path": str(root),
                    "bytes": path_size(root),
                    "reason": "experiment home; inspect before manual pruning",
                }
            )
    return reports


def runtime_backup_roots(paths: BrainPaths) -> list[Path]:
    return [paths.home / "backups", paths.home.parent / "brain-runtime-backups"]


def action(kind: str, path: Path, *, reason: str) -> dict[str, Any]:
    return {
        "kind": kind,
        "path": str(path),
        "bytes": path_size(path),
        "modified_at": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(),
        "reason": reason,
    }


def apply_prune_action(action_item: dict[str, Any], *, keep_log_rotations: int) -> None:
    path = Path(str(action_item["path"]))
    kind = str(action_item["kind"])
    if kind == "delete_file":
        path.unlink(missing_ok=True)
        return
    if kind == "delete_tree":
        shutil.rmtree(path, ignore_errors=True)
        return
    if kind == "rotate_log":
        rotate_log(path, keep=keep_log_rotations)
        return
    raise ValueError(f"unsupported prune action kind: {kind}")


def rotate_log(path: Path, *, keep: int) -> None:
    if keep <= 0:
        path.write_text("", encoding="utf-8")
        return
    oldest = path.with_name(f"{path.name}.{keep}")
    if oldest.exists():
        oldest.unlink()
    for index in range(keep - 1, 0, -1):
        source = path.with_name(f"{path.name}.{index}")
        if source.exists():
            source.rename(path.with_name(f"{path.name}.{index + 1}"))
    if path.exists():
        path.rename(path.with_name(f"{path.name}.1"))
    path.write_text("", encoding="utf-8")
