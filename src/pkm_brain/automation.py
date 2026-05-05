from __future__ import annotations

import fcntl
import json
import plistlib
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .capture import AgentLogCapture
from .paths import BrainPaths
from .service import BrainService
from .util import now_iso


LAUNCH_AGENT_LABEL = "com.pkm-brain.agent-log-ingest"


@dataclass(frozen=True)
class AutomationResult:
    started_at: str
    capture: dict[str, Any]
    ingest: dict[str, Any] | None
    skipped: bool = False
    reason: str | None = None


def run_agent_log_ingest(
    paths: BrainPaths,
    agent: str = "all",
    codex_state: Path | None = None,
    claude_projects: Path | None = None,
    opencode_db: Path | None = None,
) -> AutomationResult:
    service = BrainService(paths)
    service.init_workspace()
    lock_path = paths.logs / "agent-log-ingest.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w") as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return AutomationResult(now_iso(), {}, None, skipped=True, reason="another run is already active")
        capture_result = AgentLogCapture(
            paths,
            codex_state=codex_state,
            claude_projects=claude_projects,
            opencode_db=opencode_db,
        ).capture(agent=agent)
        ingest_result = service.ingest()
        return AutomationResult(
            started_at=now_iso(),
            capture=capture_result.__dict__,
            ingest=ingest_result.__dict__,
        )


def launch_agent_path() -> Path:
    return Path("~/Library/LaunchAgents").expanduser() / f"{LAUNCH_AGENT_LABEL}.plist"


def render_launch_agent(
    repo_path: Path,
    brain_home: Path,
    uv_path: Path,
    interval: int = 600,
) -> dict[str, Any]:
    command = (
        f"cd {repo_path} && "
        f"{uv_path} run brain automation run-agent-log-ingest --home {brain_home}"
    )
    return {
        "Label": LAUNCH_AGENT_LABEL,
        "ProgramArguments": ["/bin/zsh", "-lc", command],
        "StartInterval": interval,
        "RunAtLoad": True,
        "StandardOutPath": str(brain_home / "logs" / "launchagent.out.log"),
        "StandardErrorPath": str(brain_home / "logs" / "launchagent.err.log"),
        "WorkingDirectory": str(repo_path),
    }


def install_launch_agent(
    repo_path: Path,
    brain_home: Path,
    uv_path: Path,
    interval: int = 600,
    dry_run: bool = False,
) -> dict[str, Any]:
    plist = render_launch_agent(repo_path, brain_home, uv_path, interval)
    path = launch_agent_path()
    if dry_run:
        return {"path": str(path), "plist": plist, "installed": False}
    path.parent.mkdir(parents=True, exist_ok=True)
    brain_home.joinpath("logs").mkdir(parents=True, exist_ok=True)
    path.write_bytes(plistlib.dumps(plist, sort_keys=False))
    uid = subprocess.check_output(["id", "-u"], text=True).strip()
    subprocess.run(["launchctl", "bootout", f"gui/{uid}", str(path)], check=False, capture_output=True, text=True)
    subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(path)], check=True)
    subprocess.run(["launchctl", "enable", f"gui/{uid}/{LAUNCH_AGENT_LABEL}"], check=True)
    return {"path": str(path), "plist": plist, "installed": True}


def uninstall_launch_agent() -> dict[str, Any]:
    path = launch_agent_path()
    uid = subprocess.check_output(["id", "-u"], text=True).strip()
    subprocess.run(["launchctl", "bootout", f"gui/{uid}", str(path)], check=False, capture_output=True, text=True)
    if path.exists():
        path.unlink()
    return {"path": str(path), "installed": False}


def launch_agent_status() -> dict[str, Any]:
    path = launch_agent_path()
    uid = subprocess.check_output(["id", "-u"], text=True).strip()
    proc = subprocess.run(
        ["launchctl", "print", f"gui/{uid}/{LAUNCH_AGENT_LABEL}"],
        capture_output=True,
        text=True,
        check=False,
    )
    return {
        "path": str(path),
        "plist_exists": path.exists(),
        "loaded": proc.returncode == 0,
        "launchctl_output": proc.stdout if proc.returncode == 0 else proc.stderr,
    }


def as_jsonable(result: AutomationResult) -> dict[str, Any]:
    return json.loads(json.dumps(result.__dict__))
