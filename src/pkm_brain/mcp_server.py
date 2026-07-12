from __future__ import annotations

from .mcp_tools import call_mcp_tool
from .paths import BrainPaths
from .service import BrainService


def create_mcp(home: str | None = None):
    from mcp.server.fastmcp import FastMCP

    paths = BrainPaths.from_value(home)
    service = BrainService(paths)
    mcp = FastMCP("pkm-brain")

    @mcp.tool()
    def search_knowledge(query: str, limit: int = 10) -> dict:
        return call_mcp_tool(service, "search_knowledge", {"query": query, "limit": limit})

    @mcp.tool()
    def retrieve_context(task: str, project: str | None = None) -> dict:
        return call_mcp_tool(service, "retrieve_context", {"task": task, "project": project})

    @mcp.tool()
    def record_context_feedback(
        target_type: str,
        target_id: str,
        useful: bool,
        note: str | None = None,
    ) -> dict:
        return call_mcp_tool(
            service,
            "record_context_feedback",
            {"target_type": target_type, "target_id": target_id, "useful": useful, "note": note},
        )

    @mcp.tool()
    def get_memories(scope: str | None = None, memory_type: str | None = None, status: str | None = "active") -> list[dict]:
        return call_mcp_tool(service, "get_memories", {"scope": scope, "memory_type": memory_type, "status": status})

    @mcp.tool()
    def propose_memory(
        memory_type: str,
        scope: str,
        content: str,
        sources: list[str],
        confidence: float,
    ) -> dict:
        return call_mcp_tool(
            service,
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
        return call_mcp_tool(
            service,
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
        return call_mcp_tool(service, "get_project_context", {"project": project})

    return mcp
