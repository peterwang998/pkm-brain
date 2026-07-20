from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from .db import connection
from .paths import BrainPaths, local_node_id
from .sync_config import load_sync_config
from .title_utils import is_self_generated_codex_provider_session
from .util import file_sha256, now_iso, slugify, text_sha256

MAX_ITEM_CHARS = 4000
MAX_RAW_JSON_CHARS = 1200
SKIPPED_TEXT_KEYS = {"data"}
SKIPPED_CONTAINER_KEYS = {"snapshot", "pastedContents"}
CAPTURE_FORMAT_VERSION = "agent-md-v5"
HYPRNOTE_TRANSCRIPT_RENDER_VERSION = "chronological-speaker-turns-v2"
HYPRNOTE_MAX_TURN_WORDS = 240
HYPRNOTE_TURN_GAP_MS = 2500
HYPRNOTE_TRACK_CLOCK_MIN_OVERLAP_WORDS = 8
HYPRNOTE_TRACK_CLOCK_OVERLAP_THRESHOLD = 0.8
HYPRNOTE_TRACK_ORDER_NOTE = (
    "[Transcript note: source timestamps use overlapping speaker-track clocks; "
    "tracks are grouped by speaker and are not presented as turn order.]"
)
SENSITIVE_KEY_PATTERNS = (
    "secret",
    "token",
    "api_key",
    "apikey",
    "password",
    "credential",
    "authorization",
    "client_secret",
    "refresh_token",
    "access_token",
)
SENSITIVE_TEXT_VALUE_RE = re.compile(
    r"""(?ix)
    (
      ["']?
      \b[A-Z0-9_.-]*
      (?:SECRET|PASSWORD|API[_-]?KEY|CREDENTIAL|ACCESS[_-]?TOKEN|REFRESH[_-]?TOKEN)
      [A-Z0-9_.-]*
      \b
      ["']?
      \s*[:=]\s*
      ["']?
    )
    ([^"'\s,}\]]+)
    """
)
AUTHORIZATION_VALUE_RE = re.compile(
    r"(?i)(\bauthorization\s*[:=]\s*(?:bearer|basic)\s+)([A-Za-z0-9._~+/\-=]+)"
)
SECRET_SHAPE_RE = re.compile(
    r"(?i)\b(?:GOCSPX-[A-Za-z0-9._/-]+|ya29\.[A-Za-z0-9._/-]+|1//[A-Za-z0-9._/-]+|AIza[A-Za-z0-9._/-]+|sk-[A-Za-z0-9._/-]+)"
)


@dataclass(frozen=True)
class AgentSessionCapture:
    agent: str
    session_id: str
    title: str
    source_path: Path
    source_hash: str
    source_mtime: float | None
    source_size: int | None
    markdown: str
    source_kind: str = "agent_session_log"
    output_group: str = "agent_logs"
    warnings: list[str] = field(default_factory=list)


@dataclass
class CaptureResult:
    discovered: int = 0
    captured: int = 0
    skipped: int = 0
    exported: int = 0
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    artifacts: list[str] = field(default_factory=list)
    outbox_artifacts: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)

    def merge(self, other: "CaptureResult") -> None:
        self.discovered += other.discovered
        self.captured += other.captured
        self.skipped += other.skipped
        self.exported += other.exported
        self.errors.extend(other.errors)
        self.warnings.extend(other.warnings)
        self.artifacts.extend(other.artifacts)
        self.outbox_artifacts.extend(other.outbox_artifacts)


class AgentLogAdapter(Protocol):
    agent: str

    def capture_sessions(self) -> list[AgentSessionCapture]:
        ...


class AgentLogCapture:
    def __init__(
        self,
        paths: BrainPaths,
        codex_state: Path | None = None,
        claude_projects: Path | None = None,
        opencode_db: Path | None = None,
        hyprnote_root: Path | None = None,
        include_hyprnote: bool = False,
    ) -> None:
        self.paths = paths
        self.codex_state = codex_state or Path("~/.codex/state_5.sqlite").expanduser()
        self.claude_projects = claude_projects or Path("~/.claude/projects").expanduser()
        self.opencode_db = opencode_db or Path("~/.local/share/opencode/opencode.db").expanduser()
        self.hyprnote_root = hyprnote_root or Path("~/Library/Application Support/hyprnote").expanduser()
        self.include_hyprnote = include_hyprnote

    def adapters(self, agent: str = "all") -> list[AgentLogAdapter]:
        selected = {agent} if agent != "all" else {"codex", "claude", "opencode"}
        if agent == "all" and self.include_hyprnote:
            selected.add("hyprnote")
        adapters: list[AgentLogAdapter] = []
        if "codex" in selected:
            adapters.append(CodexAdapter(self.codex_state))
        if "claude" in selected:
            adapters.append(ClaudeAdapter(self.claude_projects))
        if "opencode" in selected:
            adapters.append(OpenCodeAdapter(self.opencode_db))
        if "hyprnote" in selected:
            adapters.append(HyprnoteAdapter(self.hyprnote_root))
        return adapters

    def capture(self, agent: str = "all", dry_run: bool = False, export_outbox: bool = False) -> CaptureResult:
        self.paths.inbox.mkdir(parents=True, exist_ok=True)
        result = CaptureResult()
        for adapter in self.adapters(agent):
            try:
                sessions = adapter.capture_sessions()
            except Exception as exc:
                result.errors.append(f"{adapter.agent}: {exc}")
                continue
            result.merge(
                self.capture_sessions(
                    sessions,
                    dry_run=dry_run,
                    export_outbox=export_outbox,
                )
            )
        return result

    def capture_sessions(
        self,
        sessions: list[AgentSessionCapture],
        *,
        dry_run: bool = False,
        export_outbox: bool = False,
    ) -> CaptureResult:
        self.paths.inbox.mkdir(parents=True, exist_ok=True)
        result = CaptureResult(discovered=len(sessions))
        for session in sessions:
            result.warnings.extend(session.warnings)
            output = self.paths.inbox / session.output_group / session.agent / f"{slugify(session.session_id)}.md"
            if self._is_unchanged(session):
                result.skipped += 1
                if export_outbox and not dry_run and output.exists():
                    try:
                        export = export_capture_to_outbox(self.paths, session, output)
                        if export.exported:
                            result.exported += 1
                        result.outbox_artifacts.append(str(export.path))
                    except Exception as exc:
                        result.errors.append(f"{session.agent}:{session.session_id}: outbox export failed: {exc}")
                continue
            if dry_run:
                result.captured += 1
                result.artifacts.append(str(output))
                continue
            output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(output.parent, 0o700)
            write_text_atomic(output, session.markdown)
            self._record_capture(session, output, "captured", None)
            result.captured += 1
            result.artifacts.append(str(output))
            if export_outbox:
                try:
                    export = export_capture_to_outbox(self.paths, session, output)
                    if export.exported:
                        result.exported += 1
                    result.outbox_artifacts.append(str(export.path))
                except Exception as exc:
                    result.errors.append(f"{session.agent}:{session.session_id}: outbox export failed: {exc}")
        return result

    def _is_unchanged(self, session: AgentSessionCapture) -> bool:
        capture_id = f"{session.agent}:{session.session_id}"
        state_hash = capture_state_hash(session.source_hash)
        with connection(self.paths.sqlite_path) as conn:
            row = conn.execute(
                """
                SELECT source_hash, status, captured_path
                FROM capture_sources
                WHERE id = ?
                """,
                (capture_id,),
            ).fetchone()
        return bool(
            row
            and row["source_hash"] == state_hash
            and row["status"] == "captured"
            and str(row["captured_path"] or "").strip()
            and Path(str(row["captured_path"])).is_file()
        )

    def _record_capture(
        self,
        session: AgentSessionCapture,
        output: Path,
        status: str,
        error: str | None,
    ) -> None:
        capture_id = f"{session.agent}:{session.session_id}"
        with connection(self.paths.sqlite_path) as conn:
            conn.execute(
                """
                INSERT INTO capture_sources(
                  id, source_kind, agent, session_id, source_path, source_hash,
                  source_mtime, source_size, captured_path, captured_at, status, error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                  source_path = excluded.source_path,
                  source_hash = excluded.source_hash,
                  source_mtime = excluded.source_mtime,
                  source_size = excluded.source_size,
                  captured_path = excluded.captured_path,
                  captured_at = excluded.captured_at,
                  status = excluded.status,
                  error = excluded.error
                """,
                (
                    capture_id,
                    session.source_kind,
                    session.agent,
                    session.session_id,
                    str(session.source_path),
                    capture_state_hash(session.source_hash),
                    session.source_mtime,
                    session.source_size,
                    str(output),
                    now_iso(),
                    status,
                    error,
                ),
            )


class CodexAdapter:
    agent = "codex"

    def __init__(self, state_db: Path) -> None:
        self.state_db = state_db.expanduser()

    def capture_sessions(self) -> list[AgentSessionCapture]:
        if not self.state_db.exists():
            return []
        conn = sqlite3.connect(self.state_db)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                """
                SELECT id, rollout_path, cwd, title, model, reasoning_effort, created_at_ms, updated_at_ms
                FROM threads
                WHERE rollout_path IS NOT NULL AND rollout_path != ''
                ORDER BY updated_at_ms DESC
                """
            ).fetchall()
        finally:
            conn.close()
        captures: list[AgentSessionCapture] = []
        for row in rows:
            rollout = Path(row["rollout_path"]).expanduser()
            if not rollout.exists():
                continue
            events = read_jsonl(rollout)
            stat = rollout.stat()
            title = row["title"] or f"Codex session {row['id']}"
            user_messages = extracted_role_messages(events, "user")
            if is_self_generated_codex_provider_session("codex", title, user_messages):
                continue
            metadata = {
                "source_type": "agent_session_log",
                "agent": "codex",
                "session_id": row["id"],
                "source_path": str(rollout),
                "captured_at": now_iso(),
                "source_updated_at": row["updated_at_ms"],
                "cwd": row["cwd"] or "Unknown",
                "title": title,
                "model": row["model"] or "Unknown",
                "reasoning_effort": row["reasoning_effort"] or "Unknown",
            }
            captures.append(
                AgentSessionCapture(
                    agent="codex",
                    session_id=row["id"],
                    title=title,
                    source_path=rollout,
                    source_hash=file_sha256(rollout),
                    source_mtime=stat.st_mtime,
                    source_size=stat.st_size,
                    markdown=render_markdown(metadata, events),
                )
            )
        return captures


class ClaudeAdapter:
    agent = "claude"

    def __init__(self, projects_dir: Path) -> None:
        self.projects_dir = projects_dir.expanduser()

    def capture_sessions(self) -> list[AgentSessionCapture]:
        if not self.projects_dir.exists():
            return []
        grouped: dict[str, list[tuple[Path, dict[str, Any]]]] = {}
        captures: list[AgentSessionCapture] = []
        for path in sorted(self.projects_dir.rglob("*.jsonl")):
            events = read_jsonl(path)
            if not events:
                continue
            for event in events:
                session_id = str(event.get("sessionId") or path.stem)
                grouped.setdefault(session_id, []).append((path, event))
        for session_id, path_events in grouped.items():
            paths = [path for path, _ in path_events]
            session_events = [event for _, event in path_events]
            stats = [path.stat() for path in set(paths)]
            source_path = paths[0]
            title = find_first_value(session_events, "aiTitle") or f"Claude session {session_id}"
            cwd = find_first_value(session_events, "cwd") or find_first_value(session_events, "project") or "Unknown"
            metadata = {
                "source_type": "agent_session_log",
                "agent": "claude",
                "session_id": session_id,
                "source_path": str(source_path),
                "captured_at": now_iso(),
                "source_updated_at": max(stat.st_mtime for stat in stats),
                "cwd": cwd,
                "title": title,
                "model": "Unknown",
                "reasoning_effort": "Unknown",
            }
            captures.append(
                AgentSessionCapture(
                    agent="claude",
                    session_id=session_id,
                    title=title,
                    source_path=source_path,
                    source_hash=text_sha256("\n".join(json.dumps(event, sort_keys=True) for event in session_events)),
                    source_mtime=max(stat.st_mtime for stat in stats),
                    source_size=sum(stat.st_size for stat in stats),
                    markdown=render_markdown(metadata, session_events),
                )
            )
        return captures


class OpenCodeAdapter:
    agent = "opencode"

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path.expanduser()

    def capture_sessions(self) -> list[AgentSessionCapture]:
        if not self.db_path.exists():
            return []
        with tempfile.TemporaryDirectory(prefix="pkm-brain-opencode-") as tmp:
            copied = Path(tmp) / "opencode.db"
            copy_sqlite_family(self.db_path, copied)
            conn = sqlite3.connect(copied)
            conn.row_factory = sqlite3.Row
            try:
                sessions = conn.execute(
                    """
                    SELECT s.id, s.title, s.directory, s.version, s.time_created, s.time_updated,
                           p.worktree, p.name AS project_name
                    FROM session s
                    LEFT JOIN project p ON p.id = s.project_id
                    ORDER BY s.time_updated DESC
                    """
                ).fetchall()
                captures: list[AgentSessionCapture] = []
                for session in sessions:
                    messages = conn.execute(
                        "SELECT * FROM message WHERE session_id = ? ORDER BY time_created, id",
                        (session["id"],),
                    ).fetchall()
                    parts = conn.execute(
                        "SELECT * FROM part WHERE session_id = ? ORDER BY time_created, id",
                        (session["id"],),
                    ).fetchall()
                    events = open_code_events(messages, parts)
                    session_payload = {
                        "session": dict(session),
                        "events": events,
                    }
                    title = session["title"] or f"OpenCode session {session['id']}"
                    cwd = session["directory"] or session["worktree"] or "Unknown"
                    metadata = {
                        "source_type": "agent_session_log",
                        "agent": "opencode",
                        "session_id": session["id"],
                        "source_path": str(self.db_path),
                        "captured_at": now_iso(),
                        "source_updated_at": session["time_updated"],
                        "cwd": cwd,
                        "title": title,
                        "model": find_model(events),
                        "provider": find_provider(events),
                        "reasoning_effort": "Unknown",
                    }
                    captures.append(
                        AgentSessionCapture(
                            agent="opencode",
                            session_id=session["id"],
                            title=title,
                            source_path=self.db_path,
                            source_hash=text_sha256(json.dumps(session_payload, sort_keys=True)),
                            source_mtime=self.db_path.stat().st_mtime,
                            source_size=self.db_path.stat().st_size,
                            markdown=render_markdown(metadata, events),
                        )
                    )
            finally:
                conn.close()
        return captures


class HyprnoteAdapter:
    agent = "hyprnote"

    def __init__(self, root: Path) -> None:
        self.root = root.expanduser()

    def capture_sessions(self) -> list[AgentSessionCapture]:
        sessions_dir = self.root / "sessions"
        if not sessions_dir.exists():
            return []
        captures: list[AgentSessionCapture] = []
        for session_dir in sorted((path for path in sessions_dir.iterdir() if path.is_dir()), key=session_modified_at, reverse=True):
            capture = self.capture_session(session_dir)
            if capture:
                captures.append(capture)
        return captures

    def capture_session(self, session_dir: Path) -> AgentSessionCapture | None:
        text_paths = [
            session_dir / "_meta.json",
            session_dir / "_memo.md",
            session_dir / "_summary.md",
            session_dir / "transcript.json",
        ]
        existing = [path for path in text_paths if path.exists()]
        if not existing:
            return None
        meta = read_json_object(session_dir / "_meta.json")
        session_id = str(meta.get("id") or session_dir.name)
        event = meta.get("event") if isinstance(meta.get("event"), dict) else {}
        title = str(meta.get("title") or event.get("title") or f"Hyprnote session {session_id}")
        participant_names, participant_paths = hyprnote_participant_names(self.root, meta)
        summary = read_text_if_exists(session_dir / "_summary.md")
        memo = read_text_if_exists(session_dir / "_memo.md")
        transcript = render_hyprnote_transcript(session_dir / "transcript.json")
        hash_paths = [*existing, *participant_paths]
        stats = [path.stat() for path in existing]
        metadata = {
            "source_type": "hyprnote_meeting",
            "agent": "hyprnote",
            "session_id": session_id,
            "source_path": str(session_dir),
            "captured_at": now_iso(),
            "source_updated_at": max(stat.st_mtime for stat in stats),
            "title": title,
            "created_at": meta.get("created_at") or "",
            "event_started_at": event.get("started_at") or "",
            "event_ended_at": event.get("ended_at") or "",
            "location": event.get("location") or "",
            "participants": ", ".join(participant_names),
            "transcript_render_version": HYPRNOTE_TRANSCRIPT_RENDER_VERSION,
        }
        source_hash = text_sha256(
            "\n".join(
                [HYPRNOTE_TRANSCRIPT_RENDER_VERSION]
                + [
                    path.read_text(encoding="utf-8", errors="replace")
                    for path in hash_paths
                ]
            )
        )
        return AgentSessionCapture(
            agent="hyprnote",
            session_id=session_id,
            title=title,
            source_path=session_dir,
            source_hash=source_hash,
            source_mtime=max(stat.st_mtime for stat in stats),
            source_size=sum(stat.st_size for stat in stats),
            markdown=render_hyprnote_markdown(metadata, summary=summary, memo=memo, transcript=transcript),
            source_kind="hyprnote_meeting",
            output_group="documents",
        )


@dataclass(frozen=True)
class OutboxExport:
    node_id: str
    path: Path
    manifest_path: Path
    relative_path: str
    content_hash: str
    exported: bool
    manifest_changed: bool


def export_capture_to_outbox(paths: BrainPaths, session: AgentSessionCapture, captured_path: Path) -> OutboxExport:
    node_id, outbox_root = outbox_destination(paths)
    relative_path = Path(session.output_group) / session.agent / f"{slugify(session.session_id)}.md"
    target = outbox_root / relative_path
    content_hash = file_sha256(captured_path)
    exported = link_or_copy_if_changed(captured_path, target, content_hash)
    manifest_path = outbox_root / "manifest.jsonl"
    manifest_changed = update_outbox_manifest(
        manifest_path,
        {
            "node_id": node_id,
            "source_kind": session.source_kind,
            "agent": session.agent,
            "session_id": session.session_id,
            "relative_path": relative_path.as_posix(),
            "content_hash": content_hash,
            "captured_at": now_iso(),
            "source_path": str(session.source_path),
        },
    )
    return OutboxExport(
        node_id=node_id,
        path=target,
        manifest_path=manifest_path,
        relative_path=relative_path.as_posix(),
        content_hash=content_hash,
        exported=exported,
        manifest_changed=manifest_changed,
    )


def outbox_destination(paths: BrainPaths) -> tuple[str, Path]:
    try:
        config = load_sync_config(paths)
    except FileNotFoundError:
        node_id = local_node_id(paths)
        return node_id, paths.outbox / node_id
    if config.role == "secondary" and config.secondary and config.secondary.outbox_path:
        return config.node_id, config.secondary.outbox_path
    return config.node_id, paths.outbox / config.node_id


def link_or_copy_if_changed(source: Path, target: Path, content_hash: str | None = None) -> bool:
    content_hash = content_hash or file_sha256(source)
    if target.exists() and file_sha256(target) == content_hash:
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.tmp")
    if tmp.exists():
        tmp.unlink()
    try:
        os.link(source, tmp)
    except OSError:
        shutil.copy2(source, tmp)
    os.replace(tmp, target)
    return True


def write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    os.chmod(path, 0o600)


def update_outbox_manifest(manifest_path: Path, row: dict[str, Any]) -> bool:
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    rows_by_path = {
        str(existing.get("relative_path")): existing
        for existing in read_manifest_rows(manifest_path)
        if existing.get("relative_path")
    }
    existing = rows_by_path.get(str(row["relative_path"]))
    if existing and existing.get("content_hash") == row["content_hash"]:
        row = existing
    rows_by_path[str(row["relative_path"])] = row
    serialized = "".join(
        json.dumps(rows_by_path[key], sort_keys=True, separators=(",", ":")) + "\n"
        for key in sorted(rows_by_path)
    )
    current = manifest_path.read_text(encoding="utf-8") if manifest_path.exists() else ""
    if current == serialized:
        return False
    tmp = manifest_path.with_suffix(".jsonl.tmp")
    tmp.write_text(serialized, encoding="utf-8")
    os.replace(tmp, manifest_path)
    return True


def read_manifest_rows(manifest_path: Path) -> list[dict[str, Any]]:
    if not manifest_path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in manifest_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        parsed = json.loads(line)
        if isinstance(parsed, dict):
            rows.append(parsed)
    return rows


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    with path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                parsed = {"type": "unparsed", "text": line}
            if isinstance(parsed, dict):
                events.append(parsed)
    return events


def render_markdown(metadata: dict[str, Any], events: list[dict[str, Any]]) -> str:
    user_messages: list[str] = []
    assistant_messages: list[str] = []
    tool_notes: list[str] = []
    raw_notes: list[str] = []

    for event in events:
        role = str(event.get("role") or event.get("type") or event.get("kind") or "").lower()
        text = extract_text(event)
        if not text:
            text = truncate_text(json.dumps(redact_large_values(event), ensure_ascii=False, sort_keys=True), MAX_RAW_JSON_CHARS)
        if role == "user":
            user_messages.append(text)
        elif role == "assistant":
            assistant_messages.append(text)
        elif any(marker in role for marker in ["tool", "command", "part", "step"]):
            tool_notes.append(f"{role}: {text}")
        else:
            raw_notes.append(f"{role or 'event'}: {text}")

    title = metadata.get("title") or f"{metadata.get('agent', 'Agent')} session"
    lines = [
        "---",
        *yaml_frontmatter_lines(metadata),
        "---",
        "",
        f"# Agent Session: {title}",
        "",
        "## Summary",
        "",
        "Mechanical capture of an agent session. No LLM synthesis has been applied.",
        "",
        "## Metadata",
        "",
    ]
    for key in ["agent", "session_id", "source_path", "cwd", "model", "provider", "reasoning_effort", "captured_at"]:
        if key in metadata:
            lines.append(f"- {key}: {metadata.get(key) or 'Unknown'}")
    lines.extend(["", "## User Requests", ""])
    lines.extend(markdown_items(user_messages))
    lines.extend(["", "## Assistant Responses", ""])
    lines.extend(markdown_items(assistant_messages))
    lines.extend(["", "## Tool / Command Activity", ""])
    lines.extend(markdown_items(tool_notes))
    lines.extend(["", "## Raw Event Notes", ""])
    lines.extend(markdown_items(raw_notes))
    lines.append("")
    return "\n".join(lines)


def render_hyprnote_markdown(metadata: dict[str, Any], summary: str, memo: str, transcript: str) -> str:
    title = metadata.get("title") or "Hyprnote meeting"
    participants = [
        value.strip()
        for value in str(metadata.get("participants") or "").split(",")
        if value.strip()
    ]
    lines = [
        "---",
        *yaml_frontmatter_lines(metadata),
        "---",
        "",
        f"# Meeting: {title}",
        "",
    ]
    if participants:
        lines.extend(
            [
                "## Known Participants",
                "",
                *[f"- {participant}" for participant in participants],
                "",
            ]
        )
    lines.extend(
        [
            "## Summary",
            "",
            summary.strip() or "No summary was captured.",
            "",
            "## Memo",
            "",
            memo.strip() or "No memo was captured.",
            "",
            "## Transcript",
            "",
            transcript.strip() or "No transcript was captured.",
            "",
        ]
    )
    return "\n".join(lines)


def yaml_frontmatter_lines(metadata: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    for key, value in metadata.items():
        safe = str(value).replace("\n", " ").replace('"', '\\"')
        lines.append(f'{key}: "{safe}"')
    return lines


def render_hyprnote_transcript(path: Path) -> str:
    if not path.exists():
        return ""
    data = read_json_object(path)
    transcripts = data.get("transcripts")
    if not isinstance(transcripts, list):
        return ""
    words: list[dict[str, Any]] = []
    stable_index = 0
    for transcript_index, transcript in enumerate(transcripts):
        if not isinstance(transcript, dict):
            continue
        transcript_words = transcript.get("words")
        if not isinstance(transcript_words, list):
            continue
        speaker_hints = hyprnote_speaker_hints(transcript)
        transcript_started_at = hyprnote_timestamp_ms(transcript.get("started_at"))
        for word_index, word in enumerate(transcript_words):
            if not isinstance(word, dict) or not str(word.get("text") or "").strip():
                continue
            start_ms = optional_number(word.get("start_ms"))
            end_ms = optional_number(word.get("end_ms"))
            absolute_start = (
                transcript_started_at + start_ms
                if transcript_started_at is not None and start_ms is not None
                else start_ms
            )
            absolute_end = (
                transcript_started_at + end_ms
                if transcript_started_at is not None and end_ms is not None
                else end_ms
            )
            word_id = str(word.get("id") or "")
            channel = word.get("channel")
            speaker_index = speaker_hints.get(word_id)
            if speaker_index is not None:
                speaker_key = f"channel:{channel}:speaker:{speaker_index}"
            elif channel is not None:
                speaker_key = f"channel:{channel}"
            else:
                speaker_key = f"transcript:{transcript_index}"
            words.append(
                {
                    "text": str(word.get("text") or ""),
                    "speaker_key": speaker_key,
                    "channel": channel,
                    "start_ms": absolute_start,
                    "end_ms": absolute_end,
                    "stable_index": stable_index,
                    "fallback_order": (transcript_index, word_index),
                }
            )
            stable_index += 1
    if not words:
        return ""
    grouped_tracks = hyprnote_uses_overlapping_track_clocks(words)
    if grouped_tracks:
        speaker_order: dict[str, int] = {}
        for word in words:
            speaker_key = str(word["speaker_key"])
            speaker_order.setdefault(speaker_key, len(speaker_order))
        words.sort(
            key=lambda word: (
                speaker_order[str(word["speaker_key"])],
                *hyprnote_word_sort_key(word),
            )
        )
    else:
        words.sort(key=hyprnote_word_sort_key)
    speaker_labels: dict[str, str] = {}
    turns: list[dict[str, Any]] = []
    for word in words:
        speaker_key = str(word["speaker_key"])
        speaker_labels.setdefault(speaker_key, f"Speaker {len(speaker_labels) + 1}")
        current = turns[-1] if turns else None
        gap_ms = hyprnote_word_gap_ms(current, word)
        if (
            current is None
            or current["speaker_key"] != speaker_key
            or len(current["words"]) >= HYPRNOTE_MAX_TURN_WORDS
            or (gap_ms is not None and gap_ms > HYPRNOTE_TURN_GAP_MS)
        ):
            current = {
                "speaker_key": speaker_key,
                "words": [],
                "end_ms": word.get("end_ms"),
            }
            turns.append(current)
        current["words"].append(str(word["text"]))
        if word.get("end_ms") is not None:
            current["end_ms"] = word["end_ms"]
    rendered = [HYPRNOTE_TRACK_ORDER_NOTE] if grouped_tracks else []
    for turn in turns:
        cleaned = join_hyprnote_word_texts(turn["words"])
        if cleaned:
            rendered.append(f"{speaker_labels[turn['speaker_key']]}: {cleaned}")
    return "\n\n".join(rendered)


def hyprnote_participant_names(
    root: Path, meta: dict[str, Any]
) -> tuple[list[str], list[Path]]:
    participants = meta.get("participants")
    if not isinstance(participants, list):
        return [], []
    names: list[str] = []
    paths: list[Path] = []
    for participant in participants:
        if not isinstance(participant, dict):
            continue
        human_id = str(participant.get("human_id") or "").strip()
        if not human_id:
            continue
        path = root / "humans" / f"{human_id}.md"
        if not path.exists():
            continue
        paths.append(path)
        name = hyprnote_human_name(path)
        if name and name not in names:
            names.append(name)
    return names, paths


def hyprnote_human_name(path: Path) -> str:
    text = path.read_text(encoding="utf-8", errors="replace")
    match = re.search(r"(?m)^name:\s*(.+?)\s*$", text)
    if match is None:
        return ""
    return match.group(1).strip().strip("'\"")


def hyprnote_speaker_hints(transcript: dict[str, Any]) -> dict[str, int]:
    output: dict[str, int] = {}
    hints = transcript.get("speaker_hints")
    if not isinstance(hints, list):
        return output
    for hint in hints:
        if not isinstance(hint, dict) or hint.get("type") != "provider_speaker_index":
            continue
        word_id = str(hint.get("word_id") or "").strip()
        if not word_id:
            continue
        try:
            value = json.loads(str(hint.get("value") or "{}"))
        except json.JSONDecodeError:
            continue
        if not isinstance(value, dict):
            continue
        speaker_index = value.get("speaker_index")
        if isinstance(speaker_index, int):
            output[word_id] = speaker_index
    return output


def hyprnote_timestamp_ms(value: Any) -> float | None:
    numeric = optional_number(value)
    if numeric is not None:
        return numeric
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp() * 1000
    except ValueError:
        return None


def optional_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


def hyprnote_word_sort_key(word: dict[str, Any]) -> tuple[Any, ...]:
    start_ms = word.get("start_ms")
    if start_ms is None:
        return (1, *word["fallback_order"], word["stable_index"])
    return (0, float(start_ms), word["stable_index"])


def hyprnote_word_gap_ms(
    current: dict[str, Any] | None, word: dict[str, Any]
) -> float | None:
    if current is None or current.get("end_ms") is None or word.get("start_ms") is None:
        return None
    return float(word["start_ms"]) - float(current["end_ms"])


def hyprnote_uses_overlapping_track_clocks(words: list[dict[str, Any]]) -> bool:
    starts_by_channel: dict[str, set[float]] = {}
    for word in words:
        channel = word.get("channel")
        start_ms = word.get("start_ms")
        if channel is None or start_ms is None:
            continue
        starts_by_channel.setdefault(str(channel), set()).add(float(start_ms))
    channels = sorted(starts_by_channel)
    for index, left_channel in enumerate(channels):
        left = starts_by_channel[left_channel]
        for right_channel in channels[index + 1 :]:
            right = starts_by_channel[right_channel]
            denominator = min(len(left), len(right))
            if denominator < HYPRNOTE_TRACK_CLOCK_MIN_OVERLAP_WORDS:
                continue
            overlap = len(left & right) / denominator
            if overlap >= HYPRNOTE_TRACK_CLOCK_OVERLAP_THRESHOLD:
                return True
    return False


def join_hyprnote_word_texts(words: list[str]) -> str:
    cleaned = " ".join(part.strip() for part in words if part.strip())
    cleaned = re.sub(r"\s+([,.;:!?%])", r"\1", cleaned)
    cleaned = re.sub(r"([([{])\s+", r"\1", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


def read_json_object(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        parsed = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def read_text_if_exists(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")


def session_modified_at(path: Path) -> float:
    try:
        return max((child.stat().st_mtime for child in path.iterdir() if child.is_file()), default=path.stat().st_mtime)
    except OSError:
        return 0.0


def markdown_items(items: list[str]) -> list[str]:
    if not items:
        return ["- Unknown"]
    rendered: list[str] = []
    for item in items:
        rendered.append("- " + truncate_text(item, MAX_ITEM_CHARS).replace("\n", "\n  "))
    return rendered


def extracted_role_messages(events: list[dict[str, Any]], expected_role: str) -> list[str]:
    messages: list[str] = []
    for event in events:
        role = str(event.get("role") or event.get("type") or event.get("kind") or "").lower()
        if role == expected_role:
            text = extract_text(event)
            if text:
                messages.append(text)
    return messages


def extract_text(value: Any) -> str:
    found: list[str] = []

    def walk(item: Any) -> None:
        if isinstance(item, dict):
            for key, nested in item.items():
                if is_sensitive_key(key):
                    found.append(f"{key}: [redacted]")
                elif key in SKIPPED_CONTAINER_KEYS:
                    found.append(f"[omitted {key}]")
                elif key in {"text", "content", "display"} and isinstance(nested, str):
                    found.append(redact_text(nested))
                elif key == "message":
                    walk(nested)
                elif key not in SKIPPED_TEXT_KEYS:
                    walk(nested)
        elif isinstance(item, list):
            for nested in item:
                walk(nested)

    walk(value)
    return "\n".join(dedupe_preserve_order(found)).strip()


def redact_text(text: str) -> str:
    stripped = text.strip()
    if looks_like_base64(stripped):
        return f"[omitted base64 payload: {len(stripped)} chars]"
    redacted = AUTHORIZATION_VALUE_RE.sub(r"\1[redacted]", stripped)
    redacted = SENSITIVE_TEXT_VALUE_RE.sub(r"\1[redacted]", redacted)
    redacted = SECRET_SHAPE_RE.sub("[redacted]", redacted)
    return redacted


def redact_large_values(value: Any) -> Any:
    if isinstance(value, dict):
        output: dict[str, Any] = {}
        for key, nested in value.items():
            if is_sensitive_key(key):
                output[key] = "[redacted]"
            elif key in SKIPPED_CONTAINER_KEYS:
                output[key] = f"[omitted {key}]"
            elif isinstance(nested, str):
                output[key] = redact_text(truncate_text(nested, MAX_RAW_JSON_CHARS))
            else:
                output[key] = redact_large_values(nested)
        return output
    if isinstance(value, list):
        return [redact_large_values(item) for item in value[:20]]
    if isinstance(value, str):
        return redact_text(truncate_text(value, MAX_RAW_JSON_CHARS))
    return value


def truncate_text(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return f"{text[:max_chars]}... [truncated {len(text) - max_chars} chars]"


def looks_like_base64(text: str) -> bool:
    if len(text) < 800:
        return False
    allowed = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=\n\r")
    if any(ch not in allowed for ch in text):
        return False
    return len(set(text.replace("\n", "").replace("\r", ""))) > 20


def is_sensitive_key(key: str) -> bool:
    normalized = key.lower().replace("-", "_")
    return any(pattern in normalized for pattern in SENSITIVE_KEY_PATTERNS)


def dedupe_preserve_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for item in items:
        stripped = item.strip()
        if stripped and stripped not in seen:
            seen.add(stripped)
            output.append(stripped)
    return output


def find_first_value(events: list[dict[str, Any]], key: str) -> str | None:
    for event in events:
        value = event.get(key)
        if value:
            return str(value)
    return None


def copy_sqlite_family(source: Path, target: Path) -> None:
    shutil.copy2(source, target)
    for suffix in ["-wal", "-shm"]:
        sidecar = source.with_name(source.name + suffix)
        if sidecar.exists():
            shutil.copy2(sidecar, target.with_name(target.name + suffix))


def open_code_events(messages: list[sqlite3.Row], parts: list[sqlite3.Row]) -> list[dict[str, Any]]:
    by_message: dict[str, list[dict[str, Any]]] = {}
    for part in parts:
        by_message.setdefault(part["message_id"], []).append(parse_json_text(part["data"]))
    events: list[dict[str, Any]] = []
    for message in messages:
        data = parse_json_text(message["data"])
        role = data.get("role", "message")
        event = {
            "type": role,
            "role": role,
            "message_id": message["id"],
            "time_created": message["time_created"],
            "model": data.get("modelID") or (data.get("model") or {}).get("modelID"),
            "provider": data.get("providerID") or (data.get("model") or {}).get("providerID"),
            "parts": by_message.get(message["id"], []),
        }
        text_parts = [part.get("text", "") for part in event["parts"] if part.get("type") == "text"]
        if text_parts:
            event["text"] = "\n".join(text_parts)
        else:
            event["text"] = json.dumps(event["parts"], ensure_ascii=False, sort_keys=True)[:2000]
        events.append(event)
    return events


def parse_json_text(value: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, dict) else {"value": parsed}
    except json.JSONDecodeError:
        return {"text": value}


def find_model(events: list[dict[str, Any]]) -> str:
    for event in events:
        if event.get("model"):
            return str(event["model"])
    return "Unknown"


def find_provider(events: list[dict[str, Any]]) -> str:
    for event in events:
        if event.get("provider"):
            return str(event["provider"])
    return "Unknown"


def capture_state_hash(source_hash: str) -> str:
    return f"{CAPTURE_FORMAT_VERSION}:{source_hash}"
