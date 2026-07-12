from __future__ import annotations

import argparse
import json
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from .daemon import read_daemon_handshake
from .mcp_tools import WRITE_TOOL_NAMES, call_mcp_tool, read_only_write_declined
from .paths import BrainPaths
from .service import BrainService, ReadOnlyModeError


@dataclass(frozen=True)
class DaemonEndpoint:
    base_url: str
    token: str


class BrainMCPProxy:
    def __init__(
        self,
        home: str | None = None,
        *,
        auto_launch: bool = True,
        app_name: str = "PKM Brain",
        request_timeout_s: float = 120.0,
    ) -> None:
        self.paths = BrainPaths.from_value(home)
        self.auto_launch = auto_launch
        self.app_name = app_name
        self.request_timeout_s = request_timeout_s
        self._read_only_session = False

    def call_tool(self, tool_name: str, payload: dict[str, Any]) -> Any:
        endpoint = None if self._read_only_session else self.resolve_endpoint()
        if endpoint:
            return self.post_tool(endpoint, tool_name, payload)
        if self.auto_launch:
            self.launch_app()
        self._read_only_session = True
        if tool_name in WRITE_TOOL_NAMES:
            return read_only_write_declined(tool_name)
        try:
            return call_mcp_tool(BrainService(self.paths, read_only=True), tool_name, payload)
        except ReadOnlyModeError as exc:
            return {"error": str(exc), "tool": tool_name, "read_only": True, "retryable": True}

    def resolve_endpoint(self) -> DaemonEndpoint | None:
        endpoint = self.endpoint_from_handshake()
        if endpoint and self.health_ok(endpoint):
            return endpoint
        return None

    def endpoint_from_handshake(self) -> DaemonEndpoint | None:
        handshake = read_daemon_handshake(self.paths)
        if not handshake:
            return None
        port = handshake.get("port")
        token = handshake.get("token")
        if not isinstance(port, int) or not isinstance(token, str) or not token:
            return None
        host = str(handshake.get("host") or "127.0.0.1")
        return DaemonEndpoint(base_url=f"http://{host}:{port}", token=token)

    def health_ok(self, endpoint: DaemonEndpoint) -> bool:
        try:
            request = urllib.request.Request(
                f"{endpoint.base_url}/api/health",
                headers={"Authorization": f"Bearer {endpoint.token}"},
                method="GET",
            )
            with urllib.request.urlopen(request, timeout=self.request_timeout_s) as response:
                if response.status != 200:
                    return False
                payload = json.loads(response.read().decode("utf-8"))
                return bool(payload.get("ok"))
        except (OSError, urllib.error.URLError, json.JSONDecodeError):
            return False

    def post_tool(self, endpoint: DaemonEndpoint, tool_name: str, payload: dict[str, Any]) -> Any:
        encoded = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            f"{endpoint.base_url}/api/mcp/{tool_name}",
            data=encoded,
            headers={
                "Authorization": f"Bearer {endpoint.token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=self.request_timeout_s) as response:
            return json.loads(response.read().decode("utf-8"))

    def launch_app(self) -> None:
        subprocess.run(
            ["open", "-g", "-a", self.app_name],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


def create_mcp_proxy(home: str | None = None, *, auto_launch: bool = True):
    from mcp.server.fastmcp import FastMCP

    proxy = BrainMCPProxy(home, auto_launch=auto_launch)
    mcp = FastMCP("pkm-brain")

    @mcp.tool()
    def search_knowledge(query: str, limit: int = 10) -> dict:
        return proxy.call_tool("search_knowledge", {"query": query, "limit": limit})

    @mcp.tool()
    def retrieve_context(task: str, project: str | None = None) -> dict:
        return proxy.call_tool("retrieve_context", {"task": task, "project": project})

    @mcp.tool()
    def record_context_feedback(
        target_type: str,
        target_id: str,
        useful: bool,
        note: str | None = None,
    ) -> dict:
        return proxy.call_tool(
            "record_context_feedback",
            {"target_type": target_type, "target_id": target_id, "useful": useful, "note": note},
        )

    @mcp.tool()
    def get_memories(scope: str | None = None, memory_type: str | None = None, status: str | None = "active") -> list[dict]:
        return proxy.call_tool("get_memories", {"scope": scope, "memory_type": memory_type, "status": status})

    @mcp.tool()
    def propose_memory(
        memory_type: str,
        scope: str,
        content: str,
        sources: list[str],
        confidence: float,
    ) -> dict:
        return proxy.call_tool(
            "propose_memory",
            {
                "memory_type": memory_type,
                "scope": scope,
                "content": content,
                "sources": sources,
                "confidence": confidence,
            },
        )

    @mcp.tool()
    def write_agent_session(
        summary: str,
        files_touched: list[str],
        commands_run: list[str],
        outcome: str,
        unresolved_issues: list[str] | None = None,
    ) -> dict:
        return proxy.call_tool(
            "write_agent_session",
            {
                "summary": summary,
                "files_touched": files_touched,
                "commands_run": commands_run,
                "outcome": outcome,
                "unresolved_issues": unresolved_issues or [],
            },
        )

    @mcp.tool()
    def get_project_context(project: str) -> dict:
        return proxy.call_tool("get_project_context", {"project": project})

    return mcp


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="brain-mcp")
    parser.add_argument("--home", default=None)
    parser.add_argument("--no-auto-launch", action="store_true")
    args = parser.parse_args(argv)
    create_mcp_proxy(args.home, auto_launch=not args.no_auto_launch).run()
