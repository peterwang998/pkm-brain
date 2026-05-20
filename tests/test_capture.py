from __future__ import annotations

import json
import plistlib
import shlex
import sqlite3
from pathlib import Path

from pkm_brain.automation import render_launch_agent, render_nightly_launch_agent, run_agent_log_ingest, run_nightly_maintenance
from pkm_brain.capture import AgentLogCapture, redact_text
from pkm_brain.db import connection
from pkm_brain.llm import CODEX_DEFAULT_MODEL, DEFAULT_LLM_PROVIDER, OPENAI_DEFAULT_MODEL, provider_status
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


def make_hyprnote_fixture(tmp_path: Path) -> Path:
    root = tmp_path / "hyprnote"
    session = root / "sessions" / "hyprnote-session-1"
    session.mkdir(parents=True)
    (session / "_meta.json").write_text(
        json.dumps(
            {
                "id": "hyprnote-session-1",
                "title": "Customer Meeting",
                "created_at": "2026-05-07T15:00:00Z",
                "event": {
                    "title": "Customer Meeting",
                    "started_at": "2026-05-07T15:00:00Z",
                    "ended_at": "2026-05-07T16:00:00Z",
                    "location": "Zoom",
                },
            }
        ),
        encoding="utf-8",
    )
    (session / "_summary.md").write_text("# Summary\n\n- Discussed Ketch integration.", encoding="utf-8")
    (session / "_memo.md").write_text("Follow up on deletion workflow.", encoding="utf-8")
    (session / "transcript.json").write_text(
        json.dumps(
            {
                "transcripts": [
                    {
                        "words": [
                            {"text": " Hello"},
                            {"text": " from"},
                            {"text": " Hyprnote."},
                        ]
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    (session / "audio.mp3").write_bytes(b"audio should not be copied")
    return root


def test_capture_agents_writes_markdown_and_skips_unchanged(tmp_path: Path) -> None:
    svc = make_service(tmp_path)
    capture = AgentLogCapture(
        svc.paths,
        codex_state=make_codex_fixture(tmp_path),
        claude_projects=make_claude_fixture(tmp_path),
        opencode_db=make_opencode_fixture(tmp_path),
        hyprnote_root=tmp_path / "missing-hyprnote",
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
        hyprnote_root=tmp_path / "missing-hyprnote",
    )
    capture.capture()

    result = svc.ingest()

    assert result.changed == 3
    with connection(svc.paths.sqlite_path) as conn:
        source_types = [row["source_type"] for row in conn.execute("SELECT source_type FROM documents")]
    assert source_types == ["agent_session_log", "agent_session_log", "agent_session_log"]


def test_capture_hyprnote_writes_meeting_markdown_and_skips_unchanged(tmp_path: Path) -> None:
    svc = make_service(tmp_path)
    capture = AgentLogCapture(
        svc.paths,
        codex_state=tmp_path / "missing-codex.sqlite",
        claude_projects=tmp_path / "missing-claude",
        opencode_db=tmp_path / "missing-opencode.sqlite",
        hyprnote_root=make_hyprnote_fixture(tmp_path),
    )

    first = capture.capture(agent="hyprnote")

    assert first.discovered == 1
    assert first.captured == 1
    artifact = svc.paths.inbox / "documents" / "hyprnote" / "hyprnote-session-1.md"
    text = artifact.read_text(encoding="utf-8")
    assert 'source_type: "hyprnote_meeting"' in text
    assert "# Meeting: Customer Meeting" in text
    assert "Discussed Ketch integration." in text
    assert "Follow up on deletion workflow." in text
    assert "Hello from Hyprnote." in text
    assert "audio should not be copied" not in text

    second = capture.capture(agent="hyprnote")
    assert second.skipped == 1
    assert second.captured == 0


def test_capture_all_does_not_include_hyprnote_unless_opted_in(tmp_path: Path) -> None:
    svc = make_service(tmp_path)
    hyprnote_root = make_hyprnote_fixture(tmp_path)
    capture = AgentLogCapture(
        svc.paths,
        codex_state=tmp_path / "missing-codex.sqlite",
        claude_projects=tmp_path / "missing-claude",
        opencode_db=tmp_path / "missing-opencode.sqlite",
        hyprnote_root=hyprnote_root,
    )

    default = capture.capture()

    assert default.discovered == 0
    assert not (svc.paths.inbox / "documents" / "hyprnote").exists()

    opted_in = AgentLogCapture(
        svc.paths,
        codex_state=tmp_path / "missing-codex.sqlite",
        claude_projects=tmp_path / "missing-claude",
        opencode_db=tmp_path / "missing-opencode.sqlite",
        hyprnote_root=hyprnote_root,
        include_hyprnote=True,
    ).capture()

    assert opted_in.discovered == 1
    assert opted_in.captured == 1


def test_captured_hyprnote_ingests_as_meeting_document(tmp_path: Path) -> None:
    svc = make_service(tmp_path)
    capture = AgentLogCapture(
        svc.paths,
        codex_state=tmp_path / "missing-codex.sqlite",
        claude_projects=tmp_path / "missing-claude",
        opencode_db=tmp_path / "missing-opencode.sqlite",
        hyprnote_root=make_hyprnote_fixture(tmp_path),
    )
    capture.capture(agent="hyprnote")

    result = svc.ingest()

    assert result.changed == 1
    with connection(svc.paths.sqlite_path) as conn:
        row = conn.execute("SELECT source_type, title FROM documents").fetchone()
    assert row["source_type"] == "hyprnote_meeting"
    assert row["title"] == "Customer Meeting"


def test_automation_run_captures_and_ingests(tmp_path: Path) -> None:
    svc = make_service(tmp_path)
    result = run_agent_log_ingest(
        svc.paths,
        codex_state=tmp_path / "missing-codex.sqlite",
        claude_projects=tmp_path / "missing-claude",
        opencode_db=tmp_path / "missing-opencode.sqlite",
        hyprnote_root=tmp_path / "missing-hyprnote",
    )

    assert result.capture["discovered"] == 0
    assert result.ingest is not None


def test_nightly_maintenance_runs_self_healing_tasks(tmp_path: Path) -> None:
    svc = make_service(tmp_path)
    result = run_nightly_maintenance(
        svc.paths,
        codex_state=tmp_path / "missing-codex.sqlite",
        claude_projects=tmp_path / "missing-claude",
        opencode_db=tmp_path / "missing-opencode.sqlite",
        hyprnote_root=tmp_path / "missing-hyprnote",
    )

    assert result.status == "success"
    assert result.summary["capture"]["discovered"] == 0
    assert result.summary["ingest"] is not None
    assert result.summary["index_status"]["documents"] == 0
    assert result.summary["provenance_check"]["errors"] == []
    assert result.summary["wiki_lint"]["errors"] == []
    assert result.summary["memory_audit"]["errors"] == []
    assert (svc.paths.wiki / "index.md").exists()
    with connection(svc.paths.sqlite_path) as conn:
        row = conn.execute("SELECT * FROM automation_runs WHERE job_name = ?", ("nightly-maintenance",)).fetchone()
    assert row is not None
    assert row["status"] == "success"


def test_nightly_maintenance_skips_when_not_due(tmp_path: Path) -> None:
    svc = make_service(tmp_path)
    first = run_nightly_maintenance(
        svc.paths,
        codex_state=tmp_path / "missing-codex.sqlite",
        claude_projects=tmp_path / "missing-claude",
        opencode_db=tmp_path / "missing-opencode.sqlite",
    )

    second = run_nightly_maintenance(svc.paths, if_due=True, due_after_hours=20)

    assert first.status == "success"
    assert second.skipped is True
    assert second.status == "skipped"
    assert second.run_id is None


def test_nightly_llm_proposals_fail_without_configured_default_provider(tmp_path: Path, monkeypatch) -> None:
    svc = make_service(tmp_path)
    monkeypatch.delenv("PKM_BRAIN_LLM_PROVIDER", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("PKM_BRAIN_OLLAMA_MODEL", raising=False)
    monkeypatch.setenv("PKM_BRAIN_CODEX_BIN", str(tmp_path / "missing-codex"))

    result = run_nightly_maintenance(
        svc.paths,
        with_llm_wiki_proposals=True,
        codex_state=tmp_path / "missing-codex.sqlite",
        claude_projects=tmp_path / "missing-claude",
        opencode_db=tmp_path / "missing-opencode.sqlite",
    )

    assert result.status == "failed"
    assert "codex login" in str(result.error)

    due_check = run_nightly_maintenance(svc.paths, if_due=True, with_llm_wiki_proposals=True)
    assert due_check.status == "failed"
    assert "codex login" in str(due_check.error)


def test_nightly_memory_proposal_flag_creates_only_proposed_memories(tmp_path: Path, monkeypatch) -> None:
    svc = make_service(tmp_path)
    session_id = svc.write_agent_session(
        "Nightly run found a repeated failure pattern.",
        ["src/pkm_brain/service.py"],
        ["uv run pytest"],
        "failed",
        ["The agent treated an unreviewed proposal as active guidance."],
    )

    class FakeProvider:
        name = "fake"
        model = "test"

        def complete(self, prompt: str) -> str:
            return (
                '{"memories": ['
                '{"content": "When reviewing Brain memories, treat proposed records as candidates until local CLI review approves them.", '
                '"scope": "agent:codex", '
                f'"source_ids": ["agent_session:{session_id}"], "confidence": 0.84}}'
                "]}"
            )

    monkeypatch.setattr("pkm_brain.automation.get_provider", lambda provider_name=None: FakeProvider())
    monkeypatch.setattr("pkm_brain.memory_proposals.get_provider", lambda provider_name=None: FakeProvider())

    result = run_nightly_maintenance(
        svc.paths,
        with_llm_memory_proposals=True,
        provider="fake",
        codex_state=tmp_path / "missing-codex.sqlite",
        claude_projects=tmp_path / "missing-claude",
        opencode_db=tmp_path / "missing-opencode.sqlite",
    )

    assert result.status == "success"
    assert result.summary["memory_proposals"]["created_count"] == 1
    with connection(svc.paths.sqlite_path) as conn:
        row = conn.execute("SELECT memory_type, status FROM memories").fetchone()
    assert row["memory_type"] == "AgentFailurePatternMemory"
    assert row["status"] == "proposed"


def test_llm_provider_status_reports_missing_configuration(monkeypatch) -> None:
    monkeypatch.delenv("PKM_BRAIN_LLM_PROVIDER", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("PKM_BRAIN_CODEX_BIN", "/missing/codex")

    default = provider_status()
    openai = provider_status("openai")

    assert default["provider"] == DEFAULT_LLM_PROVIDER
    assert default["configured"] is False
    assert "codex login" in default["missing"]
    assert openai["configured"] is False
    assert "OPENAI_API_KEY" in openai["missing"]
    assert "API billing" in openai["cost_source"]


def test_codex_provider_status_uses_local_cli() -> None:
    status = provider_status("codex")

    assert status["provider"] == "codex"
    assert status["model"] == CODEX_DEFAULT_MODEL
    assert "gpt-5.2" in status["fallback_models"]
    assert "ChatGPT plan" in status["cost_source"]
    assert isinstance(status["configured"], bool)


def test_launch_agent_plist_render() -> None:
    repo_path = Path.home() / "pkm-brain"
    brain_home = Path.home() / "brain"
    plist = render_launch_agent(
        repo_path=repo_path,
        brain_home=brain_home,
        uv_path=Path("/opt/homebrew/bin/uv"),
        interval=600,
    )
    encoded = plistlib.dumps(plist)
    decoded = plistlib.loads(encoded)

    assert decoded["Label"] == "com.pkm-brain.agent-log-ingest"
    assert decoded["StartInterval"] == 600
    assert "brain automation run-agent-log-ingest" in decoded["ProgramArguments"][-1]
    assert "--include-hyprnote" not in decoded["ProgramArguments"][-1]


def test_launch_agent_plist_render_quotes_paths_with_spaces(tmp_path: Path) -> None:
    repo_path = tmp_path / "repo with space"
    brain_home = tmp_path / "brain with space"
    plist = render_launch_agent(
        repo_path=repo_path,
        brain_home=brain_home,
        uv_path=Path("/opt/homebrew/bin/uv"),
        interval=600,
    )
    command = plist["ProgramArguments"][-1]

    assert f"cd {shlex.quote(str(repo_path))}" in command
    assert f"--home {shlex.quote(str(brain_home))}" in command


def test_launch_agent_plist_render_with_hyprnote_opt_in() -> None:
    repo_path = Path.home() / "pkm-brain"
    brain_home = Path.home() / "brain"
    plist = render_launch_agent(
        repo_path=repo_path,
        brain_home=brain_home,
        uv_path=Path("/opt/homebrew/bin/uv"),
        interval=600,
        include_hyprnote=True,
    )
    encoded = plistlib.dumps(plist)
    decoded = plistlib.loads(encoded)

    assert "--include-hyprnote" in decoded["ProgramArguments"][-1]


def test_nightly_launch_agent_plist_render() -> None:
    repo_path = Path.home() / "pkm-brain"
    brain_home = Path.home() / "brain"
    plist = render_nightly_launch_agent(
        repo_path=repo_path,
        brain_home=brain_home,
        uv_path=Path("/opt/homebrew/bin/uv"),
        interval=3600,
        due_after_hours=20,
    )
    encoded = plistlib.dumps(plist)
    decoded = plistlib.loads(encoded)

    assert decoded["Label"] == "com.pkm-brain.nightly-maintenance"
    assert decoded["StartInterval"] == 3600
    assert "brain automation nightly --if-due --due-after-hours 20" in decoded["ProgramArguments"][-1]
    assert decoded["StandardOutPath"].endswith("nightly-maintenance.out.log")


def test_nightly_launch_agent_plist_render_quotes_paths_with_spaces(tmp_path: Path) -> None:
    repo_path = tmp_path / "repo with space"
    brain_home = tmp_path / "brain with space"
    plist = render_nightly_launch_agent(
        repo_path=repo_path,
        brain_home=brain_home,
        uv_path=Path("/opt/homebrew/bin/uv"),
        interval=3600,
        due_after_hours=20,
    )
    command = plist["ProgramArguments"][-1]

    assert f"cd {shlex.quote(str(repo_path))}" in command
    assert f"--home {shlex.quote(str(brain_home))}" in command


def test_nightly_launch_agent_plist_render_with_openai_wiki_proposals() -> None:
    repo_path = Path.home() / "pkm-brain"
    brain_home = Path.home() / "brain"
    plist = render_nightly_launch_agent(
        repo_path=repo_path,
        brain_home=brain_home,
        uv_path=Path("/opt/homebrew/bin/uv"),
        interval=3600,
        due_after_hours=20,
        with_llm_wiki_proposals=True,
        provider="openai",
    )
    encoded = plistlib.dumps(plist)
    decoded = plistlib.loads(encoded)

    command = decoded["ProgramArguments"][-1]
    assert "--with-llm-wiki-proposals" in command
    assert "--provider openai" in command
    assert "OPENAI_API_KEY" not in command
    assert decoded["EnvironmentVariables"]["PKM_BRAIN_LLM_PROVIDER"] == "openai"
    assert decoded["EnvironmentVariables"]["PKM_BRAIN_OPENAI_MODEL"] == OPENAI_DEFAULT_MODEL


def test_nightly_launch_agent_plist_render_with_codex_wiki_proposals(monkeypatch) -> None:
    monkeypatch.setenv("PKM_BRAIN_CODEX_BIN", "/opt/homebrew/bin/codex")
    repo_path = Path.home() / "pkm-brain"
    brain_home = Path.home() / "brain"
    plist = render_nightly_launch_agent(
        repo_path=repo_path,
        brain_home=brain_home,
        uv_path=Path("/opt/homebrew/bin/uv"),
        interval=3600,
        due_after_hours=20,
        with_llm_wiki_proposals=True,
        provider="codex",
    )
    encoded = plistlib.dumps(plist)
    decoded = plistlib.loads(encoded)

    command = decoded["ProgramArguments"][-1]
    assert "--with-llm-wiki-proposals" in command
    assert "--provider codex" in command
    assert "OPENAI_API_KEY" not in command
    assert decoded["EnvironmentVariables"]["PKM_BRAIN_LLM_PROVIDER"] == "codex"
    assert decoded["EnvironmentVariables"]["PKM_BRAIN_CODEX_MODEL"] == CODEX_DEFAULT_MODEL
    assert decoded["EnvironmentVariables"]["PKM_BRAIN_CODEX_BIN"] == "/opt/homebrew/bin/codex"
    assert decoded["EnvironmentVariables"]["PKM_BRAIN_CODEX_CWD"] == str(repo_path)


def test_nightly_launch_agent_plist_render_uses_codex_as_default_llm_provider(monkeypatch) -> None:
    monkeypatch.setenv("PKM_BRAIN_CODEX_BIN", "/opt/homebrew/bin/codex")
    repo_path = Path.home() / "pkm-brain"
    brain_home = Path.home() / "brain"
    plist = render_nightly_launch_agent(
        repo_path=repo_path,
        brain_home=brain_home,
        uv_path=Path("/opt/homebrew/bin/uv"),
        interval=3600,
        due_after_hours=20,
        with_llm_memory_proposals=True,
    )
    decoded = plistlib.loads(plistlib.dumps(plist))

    command = decoded["ProgramArguments"][-1]
    assert "--with-llm-memory-proposals" in command
    assert "--provider codex" in command
    assert decoded["EnvironmentVariables"]["PKM_BRAIN_LLM_PROVIDER"] == "codex"
    assert decoded["EnvironmentVariables"]["PKM_BRAIN_CODEX_BIN"] == "/opt/homebrew/bin/codex"


def test_nightly_launch_agent_plist_render_with_codex_memory_proposals(monkeypatch) -> None:
    monkeypatch.setenv("PKM_BRAIN_CODEX_BIN", "/opt/homebrew/bin/codex")
    repo_path = Path.home() / "pkm-brain"
    brain_home = Path.home() / "brain"
    plist = render_nightly_launch_agent(
        repo_path=repo_path,
        brain_home=brain_home,
        uv_path=Path("/opt/homebrew/bin/uv"),
        interval=3600,
        due_after_hours=20,
        with_llm_memory_proposals=True,
        provider="codex",
    )
    encoded = plistlib.dumps(plist)
    decoded = plistlib.loads(encoded)

    command = decoded["ProgramArguments"][-1]
    assert decoded["Label"] == "com.pkm-brain.nightly-maintenance"
    assert "--with-llm-memory-proposals" in command
    assert "--provider codex" in command
    assert decoded["EnvironmentVariables"]["PKM_BRAIN_LLM_PROVIDER"] == "codex"
    assert decoded["EnvironmentVariables"]["PKM_BRAIN_CODEX_CWD"] == str(repo_path)


def test_redact_text_scrubs_embedded_secret_values() -> None:
    text = """
    GOOGLE_WORKSPACE_CLIENT_SECRET=GOCSPX-example-secret
    "refresh_token": "1//example-refresh-token"
    authorization: Bearer ya29.example-access-token
    regular_key: keep-this
    """

    redacted = redact_text(text)

    assert "GOCSPX-example-secret" not in redacted
    assert "1//example-refresh-token" not in redacted
    assert "ya29.example-access-token" not in redacted
    assert "GOOGLE_WORKSPACE_CLIENT_SECRET=[redacted]" in redacted
    assert '"refresh_token": "[redacted]"' in redacted
    assert "authorization: Bearer [redacted]" in redacted
    assert "regular_key: keep-this" in redacted
