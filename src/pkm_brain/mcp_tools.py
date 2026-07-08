from __future__ import annotations

from typing import Any

from .service import BrainService


READ_ONLY_WRITE_ERROR = "PKM Brain app is not available; write declined. Launch the app and retry."

MCP_TOOL_NAMES = {
    "search_knowledge",
    "retrieve_context",
    "record_context_feedback",
    "get_memories",
    "propose_memory",
    "write_agent_session",
    "get_project_context",
}

WRITE_TOOL_NAMES = {
    "record_context_feedback",
    "propose_memory",
    "write_agent_session",
}


def call_mcp_tool(service: BrainService, tool_name: str, payload: dict[str, Any]) -> Any:
    if tool_name == "search_knowledge":
        return service.search(
            str(payload.get("query") or ""),
            limit=int(payload.get("limit") or 10),
            caller="mcp",
        )
    if tool_name == "retrieve_context":
        return service.retrieve_context(
            task=str(payload.get("task") or ""),
            project=payload.get("project"),
        )
    if tool_name == "record_context_feedback":
        return service.record_context_feedback(
            target_type=str(payload.get("target_type") or ""),
            target_id=str(payload.get("target_id") or ""),
            useful=bool(payload.get("useful")),
            note=payload.get("note"),
        )
    if tool_name == "get_memories":
        return service.list_memories(
            scope=payload.get("scope"),
            memory_type=payload.get("memory_type"),
            status=payload.get("status", "active"),
        )
    if tool_name == "propose_memory":
        memory_id = service.propose_memory(
            str(payload.get("memory_type") or ""),
            str(payload.get("scope") or ""),
            str(payload.get("content") or ""),
            list(payload.get("sources") or []),
            float(payload.get("confidence") or 0),
        )
        return {"memory_id": memory_id, "status": "proposed"}
    if tool_name == "write_agent_session":
        session_id = service.write_agent_session(
            str(payload.get("summary") or ""),
            list(payload.get("files_touched") or []),
            list(payload.get("commands_run") or []),
            str(payload.get("outcome") or ""),
            list(payload.get("unresolved_issues") or []),
        )
        return {"session_id": session_id}
    if tool_name == "get_project_context":
        project = str(payload.get("project") or "")
        return service.retrieve_context(task=f"project context for {project}", project=project)
    raise ValueError(f"unknown MCP tool: {tool_name}")


def read_only_write_declined(tool_name: str) -> dict[str, Any]:
    return {
        "error": READ_ONLY_WRITE_ERROR,
        "tool": tool_name,
        "read_only": True,
        "retryable": True,
    }
