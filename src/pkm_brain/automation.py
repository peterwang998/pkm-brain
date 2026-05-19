from __future__ import annotations

import fcntl
import json
import os
import plistlib
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .audit import audit_memories, provenance_check
from .capture import AgentLogCapture
from .db import connection, dumps
from .llm import get_provider
from .memory_proposals import propose_failure_memories_from_sources
from .paths import BrainPaths
from .service import BrainService
from .util import new_id, now_iso
from .wiki import lint_wiki, synthesize_wiki
from .wiki_proposals import propose_from_sources


LAUNCH_AGENT_LABEL = "com.pkm-brain.agent-log-ingest"
NIGHTLY_LAUNCH_AGENT_LABEL = "com.pkm-brain.nightly-maintenance"
NIGHTLY_JOB_NAME = "nightly-maintenance"


@dataclass(frozen=True)
class AutomationResult:
    started_at: str
    capture: dict[str, Any]
    ingest: dict[str, Any] | None
    skipped: bool = False
    reason: str | None = None


@dataclass(frozen=True)
class NightlyMaintenanceResult:
    run_id: str | None
    started_at: str
    finished_at: str | None
    status: str
    due: bool
    skipped: bool
    reason: str | None
    summary: dict[str, Any]
    error: str | None = None


@dataclass(frozen=True)
class SecondaryTickResult:
    started_at: str
    capture: dict[str, Any]
    ingest: dict[str, Any] | None
    index_status: dict[str, Any] | None
    skipped: bool = False
    reason: str | None = None


def run_agent_log_ingest(
    paths: BrainPaths,
    agent: str = "all",
    codex_state: Path | None = None,
    claude_projects: Path | None = None,
    opencode_db: Path | None = None,
    hyprnote_root: Path | None = None,
    include_hyprnote: bool = False,
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
            hyprnote_root=hyprnote_root,
            include_hyprnote=include_hyprnote,
        ).capture(agent=agent)
        ingest_result = service.ingest()
        return AutomationResult(
            started_at=now_iso(),
            capture=capture_result.__dict__,
            ingest=ingest_result.__dict__,
        )


def run_secondary_tick(
    paths: BrainPaths,
    agent: str = "all",
    codex_state: Path | None = None,
    claude_projects: Path | None = None,
    opencode_db: Path | None = None,
    hyprnote_root: Path | None = None,
    include_hyprnote: bool = False,
) -> SecondaryTickResult:
    service = BrainService(paths)
    service.init_workspace()
    lock_path = paths.logs / "secondary-tick.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w") as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return SecondaryTickResult(now_iso(), {}, None, None, skipped=True, reason="another secondary tick is already active")
        capture_result = AgentLogCapture(
            paths,
            codex_state=codex_state,
            claude_projects=claude_projects,
            opencode_db=opencode_db,
            hyprnote_root=hyprnote_root,
            include_hyprnote=include_hyprnote,
        ).capture(agent=agent, export_outbox=True)
        ingest_result = service.ingest()
        status = index_status(paths, service)
        return SecondaryTickResult(
            started_at=now_iso(),
            capture=capture_result.__dict__,
            ingest=ingest_result.__dict__,
            index_status=status,
        )


def run_nightly_maintenance(
    paths: BrainPaths,
    if_due: bool = False,
    due_after_hours: int = 20,
    agent: str = "all",
    codex_state: Path | None = None,
    claude_projects: Path | None = None,
    opencode_db: Path | None = None,
    hyprnote_root: Path | None = None,
    include_hyprnote: bool = False,
    with_llm_wiki_proposals: bool = False,
    with_llm_memory_proposals: bool = False,
    provider: str | None = None,
) -> NightlyMaintenanceResult:
    service = BrainService(paths)
    service.init_workspace()
    lock_path = paths.logs / "nightly-maintenance.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    started_at = now_iso()

    with lock_path.open("w") as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return NightlyMaintenanceResult(
                run_id=None,
                started_at=started_at,
                finished_at=now_iso(),
                status="skipped",
                due=False,
                skipped=True,
                reason="another nightly run is already active",
                summary={},
            )

        if with_llm_wiki_proposals or with_llm_memory_proposals:
            try:
                get_provider(provider)
            except Exception as exc:
                return NightlyMaintenanceResult(
                    run_id=None,
                    started_at=started_at,
                    finished_at=now_iso(),
                    status="failed",
                    due=True,
                    skipped=False,
                    reason=None,
                    summary={
                        "with_llm_wiki_proposals": with_llm_wiki_proposals,
                        "with_llm_memory_proposals": with_llm_memory_proposals,
                    },
                    error=str(exc),
                )

        if if_due and not nightly_due(paths, due_after_hours):
            return NightlyMaintenanceResult(
                run_id=None,
                started_at=started_at,
                finished_at=now_iso(),
                status="skipped",
                due=False,
                skipped=True,
                reason=f"last successful nightly run is less than {due_after_hours} hours old",
                summary={"due_after_hours": due_after_hours},
            )

        run_id = new_id("automation")
        record_automation_start(paths, run_id, NIGHTLY_JOB_NAME, started_at)
        summary: dict[str, Any] = {}
        status = "success"
        error: str | None = None
        try:
            capture_result = AgentLogCapture(
                paths,
                codex_state=codex_state,
                claude_projects=claude_projects,
                opencode_db=opencode_db,
                hyprnote_root=hyprnote_root,
                include_hyprnote=include_hyprnote,
            ).capture(agent=agent)
            summary["capture"] = capture_result.__dict__

            ingest_result = service.ingest()
            summary["ingest"] = ingest_result.__dict__

            wiki_result = synthesize_wiki(paths, overwrite_generated=True)
            summary["wiki_synthesize"] = wiki_result

            summary["index_status"] = index_status(paths, service)
            summary["provenance_check"] = provenance_check(paths)
            summary["wiki_lint"] = lint_wiki(paths)
            if with_llm_wiki_proposals:
                summary["wiki_proposals"] = propose_from_sources(paths, provider_name=provider)
            if with_llm_memory_proposals:
                summary["memory_proposals"] = propose_failure_memories_from_sources(paths, provider_name=provider)
            summary["memory_audit"] = audit_memories(paths)

            errors = (
                summary["capture"].get("errors", [])
                + summary["ingest"].get("errors", [])
                + summary["wiki_synthesize"].get("lint", {}).get("errors", [])
                + summary["provenance_check"].get("errors", [])
                + summary["wiki_lint"].get("errors", [])
                + summary["memory_audit"].get("errors", [])
            )
            if errors:
                status = "failed"
                error = "; ".join(str(item) for item in errors[:10])
        except Exception as exc:
            status = "failed"
            error = str(exc)
        finished_at = now_iso()
        record_automation_finish(paths, run_id, status, finished_at, summary, error)
        return NightlyMaintenanceResult(
            run_id=run_id,
            started_at=started_at,
            finished_at=finished_at,
            status=status,
            due=True,
            skipped=False,
            reason=None,
            summary=summary,
            error=error,
        )


def index_status(paths: BrainPaths, service: BrainService | None = None) -> dict[str, Any]:
    service = service or BrainService(paths)
    service.init_workspace()
    with connection(paths.sqlite_path) as conn:
        docs = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        chunks = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        fts = conn.execute("SELECT COUNT(*) FROM chunk_fts").fetchone()[0]
        run = conn.execute("SELECT * FROM ingestion_runs ORDER BY started_at DESC LIMIT 1").fetchone()
    lancedb_exists = paths.lancedb_path.exists() and any(paths.lancedb_path.iterdir())
    return {
        "documents": docs,
        "chunks": chunks,
        "fts_rows": fts,
        "lancedb_exists": lancedb_exists,
        "embedding_provider": service.embedding_provider.name,
        "last_run": dict(run) if run else None,
    }


def nightly_due(paths: BrainPaths, due_after_hours: int) -> bool:
    last_success = last_successful_automation_run(paths, NIGHTLY_JOB_NAME)
    if not last_success:
        return True
    finished_at = parse_iso_datetime(last_success)
    return datetime.now(finished_at.tzinfo) - finished_at >= timedelta(hours=due_after_hours)


def last_successful_automation_run(paths: BrainPaths, job_name: str) -> str | None:
    with connection(paths.sqlite_path) as conn:
        row = conn.execute(
            """
            SELECT finished_at
            FROM automation_runs
            WHERE job_name = ? AND status = 'success' AND finished_at IS NOT NULL
            ORDER BY finished_at DESC
            LIMIT 1
            """,
            (job_name,),
        ).fetchone()
    return str(row["finished_at"]) if row else None


def parse_iso_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value)


def record_automation_start(paths: BrainPaths, run_id: str, job_name: str, started_at: str) -> None:
    with connection(paths.sqlite_path) as conn:
        conn.execute(
            """
            INSERT INTO automation_runs(id, job_name, started_at, status)
            VALUES (?, ?, ?, ?)
            """,
            (run_id, job_name, started_at, "running"),
        )


def record_automation_finish(
    paths: BrainPaths,
    run_id: str,
    status: str,
    finished_at: str,
    summary: dict[str, Any],
    error: str | None,
) -> None:
    with connection(paths.sqlite_path) as conn:
        conn.execute(
            """
            UPDATE automation_runs
            SET finished_at = ?, status = ?, summary = ?, error = ?
            WHERE id = ?
            """,
            (finished_at, status, dumps(summary), error, run_id),
        )


def launch_agent_path() -> Path:
    return Path("~/Library/LaunchAgents").expanduser() / f"{LAUNCH_AGENT_LABEL}.plist"


def nightly_launch_agent_path() -> Path:
    return Path("~/Library/LaunchAgents").expanduser() / f"{NIGHTLY_LAUNCH_AGENT_LABEL}.plist"


def render_launch_agent(
    repo_path: Path,
    brain_home: Path,
    uv_path: Path,
    interval: int = 600,
    include_hyprnote: bool = False,
) -> dict[str, Any]:
    hyprnote_arg = " --include-hyprnote" if include_hyprnote else ""
    command = (
        f"cd {repo_path} && "
        f"{uv_path} run brain automation run-agent-log-ingest --home {brain_home}{hyprnote_arg}"
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


def render_nightly_launch_agent(
    repo_path: Path,
    brain_home: Path,
    uv_path: Path,
    interval: int = 3600,
    due_after_hours: int = 20,
    with_llm_wiki_proposals: bool = False,
    with_llm_memory_proposals: bool = False,
    provider: str | None = None,
) -> dict[str, Any]:
    llm_args = ""
    if with_llm_wiki_proposals:
        llm_args = " --with-llm-wiki-proposals"
    if with_llm_memory_proposals:
        llm_args += " --with-llm-memory-proposals"
    if with_llm_wiki_proposals or with_llm_memory_proposals:
        if provider:
            llm_args += f" --provider {provider}"
    command = (
        f"cd {repo_path} && "
        f"{uv_path} run brain automation nightly --if-due --due-after-hours {due_after_hours} --home {brain_home}{llm_args}"
    )
    plist = {
        "Label": NIGHTLY_LAUNCH_AGENT_LABEL,
        "ProgramArguments": ["/bin/zsh", "-lc", command],
        "StartInterval": interval,
        "RunAtLoad": True,
        "StandardOutPath": str(brain_home / "logs" / "nightly-maintenance.out.log"),
        "StandardErrorPath": str(brain_home / "logs" / "nightly-maintenance.err.log"),
        "WorkingDirectory": str(repo_path),
    }
    if with_llm_wiki_proposals or with_llm_memory_proposals:
        environment = {}
        if provider:
            environment["PKM_BRAIN_LLM_PROVIDER"] = provider
        if provider == "openai":
            environment["PKM_BRAIN_OPENAI_MODEL"] = os.environ.get("PKM_BRAIN_OPENAI_MODEL", "gpt-5.5")
        if provider == "codex":
            environment["PKM_BRAIN_CODEX_MODEL"] = os.environ.get("PKM_BRAIN_CODEX_MODEL", "gpt-5.5")
            codex_bin = os.environ.get("PKM_BRAIN_CODEX_BIN") or shutil.which("codex")
            if codex_bin:
                environment["PKM_BRAIN_CODEX_BIN"] = codex_bin
            environment["PKM_BRAIN_CODEX_CWD"] = str(repo_path)
        if environment:
            plist["EnvironmentVariables"] = environment
    return plist


def install_launch_agent(
    repo_path: Path,
    brain_home: Path,
    uv_path: Path,
    interval: int = 600,
    include_hyprnote: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    plist = render_launch_agent(repo_path, brain_home, uv_path, interval, include_hyprnote=include_hyprnote)
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


def install_nightly_launch_agent(
    repo_path: Path,
    brain_home: Path,
    uv_path: Path,
    interval: int = 3600,
    due_after_hours: int = 20,
    with_llm_wiki_proposals: bool = False,
    with_llm_memory_proposals: bool = False,
    provider: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    plist = render_nightly_launch_agent(
        repo_path,
        brain_home,
        uv_path,
        interval,
        due_after_hours,
        with_llm_wiki_proposals=with_llm_wiki_proposals,
        with_llm_memory_proposals=with_llm_memory_proposals,
        provider=provider,
    )
    path = nightly_launch_agent_path()
    if dry_run:
        return {"path": str(path), "plist": plist, "installed": False}
    path.parent.mkdir(parents=True, exist_ok=True)
    brain_home.joinpath("logs").mkdir(parents=True, exist_ok=True)
    path.write_bytes(plistlib.dumps(plist, sort_keys=False))
    uid = subprocess.check_output(["id", "-u"], text=True).strip()
    subprocess.run(["launchctl", "bootout", f"gui/{uid}", str(path)], check=False, capture_output=True, text=True)
    subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(path)], check=True)
    subprocess.run(["launchctl", "enable", f"gui/{uid}/{NIGHTLY_LAUNCH_AGENT_LABEL}"], check=True)
    return {"path": str(path), "plist": plist, "installed": True}


def uninstall_launch_agent() -> dict[str, Any]:
    path = launch_agent_path()
    uid = subprocess.check_output(["id", "-u"], text=True).strip()
    subprocess.run(["launchctl", "bootout", f"gui/{uid}", str(path)], check=False, capture_output=True, text=True)
    if path.exists():
        path.unlink()
    return {"path": str(path), "installed": False}


def uninstall_nightly_launch_agent() -> dict[str, Any]:
    path = nightly_launch_agent_path()
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


def nightly_launch_agent_status() -> dict[str, Any]:
    path = nightly_launch_agent_path()
    uid = subprocess.check_output(["id", "-u"], text=True).strip()
    proc = subprocess.run(
        ["launchctl", "print", f"gui/{uid}/{NIGHTLY_LAUNCH_AGENT_LABEL}"],
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


def as_jsonable(result: AutomationResult | NightlyMaintenanceResult | SecondaryTickResult) -> dict[str, Any]:
    return json.loads(json.dumps(result.__dict__))
