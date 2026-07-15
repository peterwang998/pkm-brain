from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from pkm_brain.daemon import atomic_write_private_json, daemon_handshake_path
from pkm_brain.db import connection
from pkm_brain.mcp_proxy import BrainMCPProxy, DaemonEndpoint
from pkm_brain.paths import BrainPaths
from pkm_brain.service import BrainService
from pkm_brain.ui_server import create_ui_server, ensure_ui_token


class RecordingLaunchProxy(BrainMCPProxy):
    def __init__(self, home: str) -> None:
        super().__init__(home, auto_launch=True)
        self.launches = 0

    def launch_app(self) -> None:
        self.launches += 1


@contextmanager
def running_ui_with_handshake(paths: BrainPaths) -> Iterator[tuple[str, int, str]]:
    token = ensure_ui_token(paths)
    server = create_ui_server(paths, "127.0.0.1", 0, token=token)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        atomic_write_private_json(
            daemon_handshake_path(paths),
            {
                "pid": os.getpid(),
                "port": int(port),
                "token": token,
                "version": "0.1.0",
                "home": str(paths.home),
                "started_at": "2026-07-08T00:00:00+00:00",
                "host": str(host),
            },
        )
        yield str(host), int(port), token
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        daemon_handshake_path(paths).unlink(missing_ok=True)


def test_mcp_proxy_uses_daemon_passthrough_for_writes(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()

    with running_ui_with_handshake(paths):
        result = BrainMCPProxy(str(paths.home), auto_launch=False).call_tool(
            "propose_memory",
            {
                "memory_type": "FactMemory",
                "scope": "project:pkm-brain",
                "content": "Daemon MCP passthrough writes through the service layer.",
                "sources": ["test"],
                "confidence": 0.9,
            },
        )

    assert result["status"] == "proposed"
    with connection(paths.sqlite_path) as conn:
        row = conn.execute("SELECT content FROM memories WHERE id = ?", (result["memory_id"],)).fetchone()
    assert row["content"] == "Daemon MCP passthrough writes through the service layer."


def test_mcp_proxy_read_only_fallback_does_not_record_retrieval_events(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()

    result = BrainMCPProxy(str(paths.home), auto_launch=False).call_tool(
        "retrieve_context",
        {"task": "find context while app is down", "project": "pkm-brain"},
    )

    assert result["retrieval_event_id"] is None
    with connection(paths.sqlite_path) as conn:
        count = conn.execute("SELECT COUNT(*) FROM retrieval_events").fetchone()[0]
    assert count == 0


def test_mcp_proxy_read_only_fallback_declines_writes(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()

    result = BrainMCPProxy(str(paths.home), auto_launch=False).call_tool(
        "write_agent_session",
        {
            "summary": "should not write",
            "files_touched": [],
            "commands_run": [],
            "outcome": "declined",
        },
    )

    assert result["read_only"] is True
    assert "write declined" in result["error"]
    with connection(paths.sqlite_path) as conn:
        count = conn.execute("SELECT COUNT(*) FROM agent_sessions").fetchone()[0]
    assert count == 0


def test_mcp_proxy_fails_closed_for_daemon_only_mail_tools(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()
    proxy = BrainMCPProxy(str(paths.home), auto_launch=False)

    search = proxy.call_tool("search_mail", {"query": "quarterly plan"})
    thread = proxy.call_tool("get_mail_thread", {"thread_id": "thread-1"})

    assert search == {
        "error": (
            "PKM Brain app is not available; encrypted Gmail history requires "
            "the local daemon. Launch the app and retry."
        ),
        "code": "daemon_unavailable",
        "tool": "search_mail",
        "daemon_required": True,
        "retryable": True,
    }
    assert thread["code"] == "daemon_unavailable"
    assert thread["tool"] == "get_mail_thread"
    assert thread["daemon_required"] is True


def test_mcp_proxy_mail_tool_recovers_after_daemon_becomes_available(
    tmp_path: Path, monkeypatch
) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()
    proxy = BrainMCPProxy(str(paths.home), auto_launch=False)
    endpoint = DaemonEndpoint("http://127.0.0.1:4567", "test-token")
    resolutions = iter([None, endpoint])
    calls: list[tuple[DaemonEndpoint, str, dict]] = []
    monkeypatch.setattr(proxy, "resolve_endpoint", lambda: next(resolutions))
    monkeypatch.setattr(
        proxy,
        "post_tool",
        lambda current, tool, payload: (
            calls.append((current, tool, payload))
            or {"results": [], "content_trust": "untrusted_external_content"}
        ),
    )

    first = proxy.call_tool("search_mail", {"query": "planning"})
    second = proxy.call_tool("search_mail", {"query": "planning"})

    assert first["code"] == "daemon_unavailable"
    assert second["results"] == []
    assert calls == [(endpoint, "search_mail", {"query": "planning"})]


def test_mcp_proxy_auto_launches_but_returns_read_only_fallback(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()
    proxy = RecordingLaunchProxy(str(paths.home))

    read_result = proxy.call_tool(
        "retrieve_context",
        {"task": "answer after force quit", "project": "pkm-brain"},
    )
    write_result = proxy.call_tool(
        "propose_memory",
        {
            "memory_type": "FactMemory",
            "scope": "project:pkm-brain",
            "content": "must not write through fallback",
            "sources": ["test"],
            "confidence": 0.9,
        },
    )

    assert proxy.launches == 2
    assert read_result["retrieval_event_id"] is None
    assert write_result["read_only"] is True
    assert "write declined" in write_result["error"]


def test_mcp_proxy_stays_read_only_for_session_after_fallback(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()
    proxy = RecordingLaunchProxy(str(paths.home))

    read_result = proxy.call_tool(
        "retrieve_context",
        {"task": "start while app is down", "project": "pkm-brain"},
    )

    with running_ui_with_handshake(paths):
        write_result = proxy.call_tool(
            "propose_memory",
            {
                "memory_type": "FactMemory",
                "scope": "project:pkm-brain",
                "content": "must not become writable mid-session",
                "sources": ["test"],
                "confidence": 0.9,
            },
        )

    assert read_result["retrieval_event_id"] is None
    assert write_result["read_only"] is True
    with connection(paths.sqlite_path) as conn:
        count = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
    assert count == 0


def test_daemon_mcp_endpoint_requires_known_tool(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()

    with running_ui_with_handshake(paths):
        proxy = BrainMCPProxy(str(paths.home), auto_launch=False)
        endpoint = proxy.endpoint_from_handshake()
        assert endpoint is not None
        result = proxy.post_tool(
            endpoint,
            "get_memories",
            {"status": "active"},
        )

    assert result == []
