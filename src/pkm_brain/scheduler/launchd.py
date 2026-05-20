from __future__ import annotations

import plistlib
import shlex
import subprocess
from pathlib import Path
from typing import Any

from . import JobStatus, ScheduledJob


SYNC_PRIMARY_LABEL = "com.pkm-brain.sync-primary"
CAPTURE_SECONDARY_LABEL = "com.pkm-brain.capture-secondary"


class LaunchdScheduler:
    def install(self, job: ScheduledJob, dry_run: bool = False) -> dict[str, Any]:
        plist = render_plist(job)
        path = launch_agent_path(job.label)
        if dry_run:
            return {"path": str(path), "plist": plist, "installed": False}
        path.parent.mkdir(parents=True, exist_ok=True)
        job.brain_home.joinpath("logs").mkdir(parents=True, exist_ok=True)
        path.write_bytes(plistlib.dumps(plist, sort_keys=False))
        uid = user_id()
        subprocess.run(["launchctl", "bootout", f"gui/{uid}", str(path)], check=False, capture_output=True, text=True)
        subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(path)], check=True)
        subprocess.run(["launchctl", "enable", f"gui/{uid}/{job.label}"], check=True)
        return {"path": str(path), "plist": plist, "installed": True}

    def uninstall(self, label: str) -> dict[str, Any]:
        path = launch_agent_path(label)
        uid = user_id()
        subprocess.run(["launchctl", "bootout", f"gui/{uid}", str(path)], check=False, capture_output=True, text=True)
        if path.exists():
            path.unlink()
        return {"label": label, "path": str(path), "installed": False}

    def status(self, label: str | None = None) -> list[JobStatus]:
        labels = [label] if label else [SYNC_PRIMARY_LABEL, CAPTURE_SECONDARY_LABEL]
        uid = user_id()
        statuses: list[JobStatus] = []
        for current_label in labels:
            path = launch_agent_path(current_label)
            proc = subprocess.run(
                ["launchctl", "print", f"gui/{uid}/{current_label}"],
                capture_output=True,
                text=True,
                check=False,
            )
            statuses.append(
                JobStatus(
                    label=current_label,
                    path=str(path),
                    plist_exists=path.exists(),
                    loaded=proc.returncode == 0,
                    detail=proc.stdout if proc.returncode == 0 else proc.stderr,
                )
            )
        return statuses


def sync_primary_job(repo_path: Path, brain_home: Path, uv_path: Path, peer: str, interval: int = 1800) -> ScheduledJob:
    args = [str(uv_path), "run", "brain", "sync", "run", peer, "--if-reachable", "--home", str(brain_home)]
    command = f"cd {shlex.quote(str(repo_path))} && {shlex.join(args)}"
    return ScheduledJob(
        label=SYNC_PRIMARY_LABEL,
        command=command,
        interval=interval,
        brain_home=brain_home,
        repo_path=repo_path,
        stdout_path=brain_home / "logs" / "sync-primary.out.log",
        stderr_path=brain_home / "logs" / "sync-primary.err.log",
    )


def secondary_capture_job(repo_path: Path, brain_home: Path, uv_path: Path, interval: int = 600) -> ScheduledJob:
    args = [str(uv_path), "run", "brain", "automation", "secondary-tick", "--home", str(brain_home)]
    command = f"cd {shlex.quote(str(repo_path))} && {shlex.join(args)}"
    return ScheduledJob(
        label=CAPTURE_SECONDARY_LABEL,
        command=command,
        interval=interval,
        brain_home=brain_home,
        repo_path=repo_path,
        stdout_path=brain_home / "logs" / "capture-secondary.out.log",
        stderr_path=brain_home / "logs" / "capture-secondary.err.log",
    )


def render_plist(job: ScheduledJob) -> dict[str, Any]:
    return {
        "Label": job.label,
        "ProgramArguments": ["/bin/zsh", "-lc", job.command],
        "StartInterval": job.interval,
        "RunAtLoad": True,
        "StandardOutPath": str(job.stdout_path),
        "StandardErrorPath": str(job.stderr_path),
        "WorkingDirectory": str(job.repo_path),
    }


def launch_agent_path(label: str) -> Path:
    return Path("~/Library/LaunchAgents").expanduser() / f"{label}.plist"


def user_id() -> str:
    return subprocess.check_output(["id", "-u"], text=True).strip()
