from __future__ import annotations

from .paths import BrainPaths
from .service import BrainService


def create_mcp(home: str | None = None):
    from mcp.server.fastmcp import FastMCP

    paths = BrainPaths.from_value(home)
    service = BrainService(paths)
    mcp = FastMCP("pkm-brain")

    @mcp.tool()
    def search_knowledge(query: str, limit: int = 10) -> dict:
        return service.search(query, limit=limit, caller="mcp")

    @mcp.tool()
    def retrieve_context(
        task: str,
        project: str | None = None,
        budget: int | None = None,
        mode: str = "default",
        debug: bool = False,
    ) -> dict:
        return service.retrieve_context(task=task, project=project, budget=budget, mode=mode, debug=debug)

    @mcp.tool()
    def record_context_feedback(
        target_type: str,
        target_id: str,
        useful: bool,
        note: str | None = None,
    ) -> dict:
        return service.record_context_feedback(target_type=target_type, target_id=target_id, useful=useful, note=note)

    @mcp.tool()
    def get_memories(scope: str | None = None, memory_type: str | None = None, status: str | None = "active") -> list[dict]:
        return service.list_memories(scope=scope, memory_type=memory_type, status=status)

    @mcp.tool()
    def propose_memory(
        memory_type: str,
        scope: str,
        content: str,
        sources: list[str],
        confidence: float,
    ) -> dict:
        memory_id = service.propose_memory(memory_type, scope, content, sources, confidence)
        return {"memory_id": memory_id, "status": "proposed"}

    @mcp.tool()
    def write_agent_session(
        summary: str,
        files_touched: list[str],
        commands_run: list[str],
        outcome: str,
        unresolved_issues: list[str] | None = None,
    ) -> dict:
        session_id = service.write_agent_session(
            summary,
            files_touched,
            commands_run,
            outcome,
            unresolved_issues or [],
        )
        return {"session_id": session_id}

    @mcp.tool()
    def get_project_context(project: str) -> dict:
        return service.retrieve_context(task=f"project context for {project}", project=project)

    return mcp
