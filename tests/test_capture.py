from __future__ import annotations

import json
import plistlib
import sqlite3
from pathlib import Path

from pkm_brain.automation import render_launch_agent, run_agent_log_ingest
from pkm_brain.capture import AgentLogCapture
from pkm_brain.db import connection
from pkm_brain.paths import BrainPaths
from pkm_brain.service import BrainService


def make_service(tmp_path: Path) -> BrainService:
    svc = BrainService(BrainPaths.from_value(tmp_path / "brain"), prefer_model_embeddings=False)
    svc.init_workspace()
    return svc


def make_codex_fixture(tmp_path: Path) -> Path:
    state = tmp_path / "codex-state.sqlite"
    rollout = tmp_path / "rollout.jsonl"
    rollout.write_text(
        json.dumps({"type": "user", "text": "Build ingestion automation"}) + "\n"
        + json.dumps({"type": "assistant", "text": "Implemented capture adapters"}) + "\n",
        encoding="utf-8",
    )
    conn = sqlite3.connect(state)
    conn.execute(
        """
        CREATE TABLE threads(
          id TEXT PRIMARY KEY,
          rollout_path TEXT,
          cwd TEXT,
          title TEXT,
          model TEXT,
          reasoning_effort TEXT,
          created_at_ms INTEGER,
          updated_at_ms INTEGER
        )
        """
    )
    conn.execute(
        "INSERT INTO threads VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("codex-session-1", str(rollout), "/repo", "Codex Capture", "gpt-test", "high", 1, 2),
    )
    conn.commit()
    conn.close()
    return state


def make_claude_fixture(tmp_path: Path) -> Path:
    projects = tmp_path / "claude" / "projects" / "-repo"
    projects.mkdir(parents=True)
    session = projects / "claude-session.jsonl"
    session.write_text(
        json.dumps({"type": "ai-title", "sessionId": "claude-session-1", "aiTitle": "Claude Capture"}) + "\n"
        + json.dumps({"type": "user", "sessionId": "claude-session-1", "message": {"content": "Review plan"}})
        + "\n"
        + json.dumps(
            {"type": "assistant", "sessionId": "claude-session-1", "message": {"content": "Plan reviewed"}}
        )
        + "\n",
        encoding="utf-8",
    )
    return tmp_path / "claude" / "projects"


def make_opencode_fixture(tmp_path: Path) -> Path:
    db = tmp_path / "opencode.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE project(id TEXT PRIMARY KEY, worktree TEXT, name TEXT)")
    conn.execute(
        """
        CREATE TABLE session(
          id TEXT PRIMARY KEY,
          project_id TEXT,
          title TEXT,
          directory TEXT,
          version TEXT,
          time_created INTEGER,
          time_updated INTEGER
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE message(
          id TEXT PRIMARY KEY,
          session_id TEXT,
          time_created INTEGER,
          time_updated INTEGER,
          data TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE part(
          id TEXT PRIMARY KEY,
          message_id TEXT,
          session_id TEXT,
          time_created INTEGER,
          time_updated INTEGER,
          data TEXT
        )
        """
    )
    conn.execute("INSERT INTO project VALUES (?, ?, ?)", ("global", "/repo", "Repo"))
    conn.execute("INSERT INTO session VALUES (?, ?, ?, ?, ?, ?, ?)", ("opencode-session-1", "global", "OpenCode Capture", "/repo", "1", 1, 2))
    conn.execute(
        "INSERT INTO message VALUES (?, ?, ?, ?, ?)",
        ("msg-user", "opencode-session-1", 1, 1, json.dumps({"role": "user", "model": {"providerID": "opencode", "modelID": "test"}})),
    )
    conn.execute(
        "INSERT INTO part VALUES (?, ?, ?, ?, ?, ?)",
        ("part-user", "msg-user", "opencode-session-1", 1, 1, json.dumps({"type": "text", "text": "Use LaunchAgent"})),
    )
    conn.commit()
    conn.close()
    return db


def test_capture_agents_writes_markdown_and_skips_unchanged(tmp_path: Path) -> None:
    svc = make_service(tmp_path)
    capture = AgentLogCapture(
        svc.paths,
        codex_state=make_codex_fixture(tmp_path),
        claude_projects=make_claude_fixture(tmp_path),
        opencode_db=make_opencode_fixture(tmp_path),
    )

    dry = capture.capture(dry_run=True)
    assert dry.discovered == 3
    assert dry.captured == 3
    assert not list((svc.paths.inbox / "agent_logs").glob("**/*.md"))

    first = capture.capture()
    assert first.captured == 3
    artifacts = list((svc.paths.inbox / "agent_logs").glob("**/*.md"))
    assert len(artifacts) == 3
    assert all("source_type: \"agent_session_log\"" in path.read_text(encoding="utf-8") for path in artifacts)

    second = capture.capture()
    assert second.skipped == 3
    assert second.captured == 0


def test_captured_agent_logs_ingest_as_agent_session_logs(tmp_path: Path) -> None:
    svc = make_service(tmp_path)
    capture = AgentLogCapture(
        svc.paths,
        codex_state=make_codex_fixture(tmp_path),
        claude_projects=make_claude_fixture(tmp_path),
        opencode_db=make_opencode_fixture(tmp_path),
    )
    capture.capture()

    result = svc.ingest()

    assert result.changed == 3
    with connection(svc.paths.sqlite_path) as conn:
        source_types = [row["source_type"] for row in conn.execute("SELECT source_type FROM documents")]
    assert source_types == ["agent_session_log", "agent_session_log", "agent_session_log"]


def test_automation_run_captures_and_ingests(tmp_path: Path) -> None:
    svc = make_service(tmp_path)
    result = run_agent_log_ingest(
        svc.paths,
        codex_state=tmp_path / "missing-codex.sqlite",
        claude_projects=tmp_path / "missing-claude",
        opencode_db=tmp_path / "missing-opencode.sqlite",
    )

    assert result.capture["discovered"] == 0
    assert result.ingest is not None


def test_launch_agent_plist_render() -> None:
    plist = render_launch_agent(
        repo_path=Path("/Users/Peter/pkm-brain"),
        brain_home=Path("/Users/Peter/brain"),
        uv_path=Path("/opt/homebrew/bin/uv"),
        interval=600,
    )
    encoded = plistlib.dumps(plist)
    decoded = plistlib.loads(encoded)

    assert decoded["Label"] == "com.pkm-brain.agent-log-ingest"
    assert decoded["StartInterval"] == 600
    assert "brain automation run-agent-log-ingest" in decoded["ProgramArguments"][-1]
