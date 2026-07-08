from __future__ import annotations

import os
import plistlib
import shutil
import stat
import subprocess
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .automation import LAUNCH_AGENT_LABEL, NIGHTLY_LAUNCH_AGENT_LABEL
from .paths import BrainPaths
from .regeneration import backup_runtime_brain


SYNC_PRIMARY_LABEL = "com.pkm-brain.sync-primary"
CAPTURE_SECONDARY_LABEL = "com.pkm-brain.capture-secondary"
LEGACY_LAUNCH_AGENT_LABELS = (
    LAUNCH_AGENT_LABEL,
    NIGHTLY_LAUNCH_AGENT_LABEL,
    SYNC_PRIMARY_LABEL,
    CAPTURE_SECONDARY_LABEL,
)
PRIMARY_LABELS = {LAUNCH_AGENT_LABEL, NIGHTLY_LAUNCH_AGENT_LABEL, SYNC_PRIMARY_LABEL}
SECONDARY_LABELS = {CAPTURE_SECONDARY_LABEL, NIGHTLY_LAUNCH_AGENT_LABEL}

Runner = Callable[..., subprocess.CompletedProcess[str]]


def default_app_support_dir() -> Path:
    return Path("~/Library/Application Support/PKM Brain").expanduser()


def default_launch_agents_dir() -> Path:
    return Path("~/Library/LaunchAgents").expanduser()


def detect_launch_agents(launch_agents_dir: Path | None = None) -> list[dict[str, Any]]:
    launch_agents_dir = launch_agents_dir or default_launch_agents_dir()
    found: list[dict[str, Any]] = []
    for label in LEGACY_LAUNCH_AGENT_LABELS:
        path = launch_agents_dir / f"{label}.plist"
        if not path.exists():
            continue
        plist_label = label
        try:
            with path.open("rb") as fh:
                decoded = plistlib.load(fh)
            plist_label = str(decoded.get("Label") or label)
        except Exception:
            decoded = {}
        if plist_label != label:
            continue
        found.append(
            {
                "label": label,
                "plist_path": str(path),
                "role_set": role_set_for_label(label),
                "start_interval": decoded.get("StartInterval") if isinstance(decoded, dict) else None,
            }
        )
    return found


def role_set_for_label(label: str) -> str:
    if label == CAPTURE_SECONDARY_LABEL:
        return "secondary"
    if label == SYNC_PRIMARY_LABEL:
        return "primary"
    return "shared"


def migration_state(paths: BrainPaths, launch_agents_dir: Path | None = None) -> str:
    if paths.home.exists() and detect_launch_agents(launch_agents_dir):
        return "migrate"
    if paths.home.exists():
        return "adopt"
    return "fresh"


def build_migration_plan(
    paths: BrainPaths,
    *,
    app_support_dir: Path | None = None,
    launch_agents_dir: Path | None = None,
) -> dict[str, Any]:
    app_support_dir = app_support_dir or default_app_support_dir()
    launch_agents_dir = launch_agents_dir or default_launch_agents_dir()
    detected = detect_launch_agents(launch_agents_dir)
    return {
        "state": migration_state(paths, launch_agents_dir),
        "home": str(paths.home),
        "home_exists": paths.home.exists(),
        "app_support": str(app_support_dir),
        "launch_agents_dir": str(launch_agents_dir),
        "detected_launch_agents": detected,
        "rollback_script": str(rollback_script_path(app_support_dir)),
        "shim_dir": str(app_support_dir / "bin"),
        "steps": migration_steps(detected, paths.home.exists()),
    }


def migration_steps(detected_launch_agents: list[dict[str, Any]], home_exists: bool) -> list[dict[str, Any]]:
    steps = [
        {"id": "preflight", "label": "Preflight", "required": True},
        {"id": "backup", "label": "Runtime backup", "required": home_exists},
        {
            "id": "retire_launch_agents",
            "label": "Retire legacy LaunchAgents",
            "required": bool(detected_launch_agents),
        },
        {"id": "adopt_home", "label": "Adopt brain home", "required": home_exists},
        {"id": "login_item", "label": "Enable login item", "required": False},
        {"id": "agent_access", "label": "Update agent MCP registrations", "required": False},
        {"id": "cli_shims", "label": "Install CLI and MCP shims", "required": True},
        {"id": "verification", "label": "Verification checklist", "required": True},
    ]
    return steps


def create_runtime_backup(paths: BrainPaths, *, app_support_dir: Path | None = None) -> dict[str, Any]:
    app_support_dir = app_support_dir or default_app_support_dir()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output_dir = app_support_dir / "migration" / "runtime-backups" / stamp
    result = backup_runtime_brain(paths, output_dir=output_dir)
    return {"ok": True, "backup": result}


def retire_launch_agents(
    *,
    app_support_dir: Path | None = None,
    launch_agents_dir: Path | None = None,
    dry_run: bool = True,
    runner: Runner = subprocess.run,
) -> dict[str, Any]:
    app_support_dir = app_support_dir or default_app_support_dir()
    launch_agents_dir = launch_agents_dir or default_launch_agents_dir()
    detected = detect_launch_agents(launch_agents_dir)
    backup_dir = app_support_dir / "migration" / "plists-backup"
    actions: list[dict[str, Any]] = []
    for item in detected:
        source = Path(str(item["plist_path"]))
        target = backup_dir / source.name
        actions.append(
            {
                "label": item["label"],
                "source": str(source),
                "backup": str(target),
                "bootout_target": f"gui/{os.getuid()}/{item['label']}",
            }
        )
    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "detected_launch_agents": detected,
            "actions": actions,
            "rollback_script": str(rollback_script_path(app_support_dir)),
        }

    backup_dir.mkdir(parents=True, exist_ok=True)
    completed: list[dict[str, Any]] = []
    for action in actions:
        label = str(action["label"])
        source = Path(str(action["source"]))
        target = Path(str(action["backup"]))
        runner(
            ["launchctl", "bootout", f"gui/{os.getuid()}/{label}"],
            check=False,
            capture_output=True,
            text=True,
        )
        if source.exists():
            if target.exists():
                target.unlink()
            shutil.move(str(source), str(target))
        completed.append({**action, "moved": target.exists()})
    script = write_rollback_script(backup_dir, completed)
    return {
        "ok": True,
        "dry_run": False,
        "retired": completed,
        "rollback_script": str(script),
    }


def rollback_script_path(app_support_dir: Path) -> Path:
    return app_support_dir / "migration" / "plists-backup" / "rollback.sh"


def write_rollback_script(backup_dir: Path, actions: Sequence[dict[str, Any]]) -> Path:
    lines = [
        "#!/bin/zsh",
        "set -euo pipefail",
        'UID_VALUE="${UID:-$(id -u)}"',
    ]
    for action in actions:
        label = str(action["label"])
        plist = backup_dir / Path(str(action["backup"])).name
        lines.extend(
            [
                f'launchctl bootstrap "gui/$UID_VALUE" "{plist}" || true',
                f'launchctl enable "gui/$UID_VALUE/{label}" || true',
            ]
        )
    script = backup_dir / "rollback.sh"
    script.write_text("\n".join(lines) + "\n", encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


def install_runtime_shims(app_support_dir: Path | None = None) -> dict[str, Any]:
    app_support_dir = app_support_dir or default_app_support_dir()
    bin_dir = app_support_dir / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    installed = []
    for name in ("brain", "brain-mcp"):
        path = bin_dir / name
        target = f'runtime/current/bin/{name}'
        script = (
            "#!/bin/zsh\n"
            'set -euo pipefail\n'
            'ROOT="$(cd "$(dirname "$0")/.." && pwd)"\n'
            f'exec "$ROOT/{target}" "$@"\n'
        )
        path.write_text(script, encoding="utf-8")
        path.chmod(0o755)
        installed.append(str(path))
    return {"ok": True, "shim_dir": str(bin_dir), "installed": installed}
