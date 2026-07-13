from __future__ import annotations

import fcntl
import json
import threading
from collections import deque
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .util import new_id, now_iso

if TYPE_CHECKING:
    from .paths import BrainPaths


LLM_USAGE_LOG_FILENAME = "llm-usage.log"
LLM_USAGE_SCHEMA_VERSION = 1
MAX_USAGE_EVENTS_TO_SUMMARIZE = 50_000
USAGE_CATEGORY_BY_ROLE = {
    "extractor": "extractor",
    "critic": "evaluator",
    "auditor": "auditor",
}
_USAGE_WRITE_LOCK = threading.Lock()


def configure_provider_usage(
    provider: Any,
    paths: BrainPaths,
    role: str,
    *,
    cycle_id: str | None = None,
    run_id: str | None = None,
    stage: str | None = None,
) -> Any:
    context = {
        "log_path": str(llm_usage_log_path(paths)),
        "role": role,
        "usage_category": USAGE_CATEGORY_BY_ROLE.get(role, role),
        "cycle_id": cycle_id or run_id or new_id("llmcycle"),
        "run_id": run_id,
        "stage": stage or role,
    }
    try:
        setattr(provider, "_pkm_usage_context", context)
    except (AttributeError, TypeError):
        pass
    return provider


def llm_usage_log_path(paths: BrainPaths) -> Path:
    return paths.logs / LLM_USAGE_LOG_FILENAME


def record_provider_usage(
    provider: Any,
    *,
    model: str,
    usage: dict[str, Any] | None,
    status: str,
    started_at: str,
    duration_ms: int,
    error_type: str | None = None,
    session_id: str | None = None,
    rate_limits: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    context = getattr(provider, "_pkm_usage_context", None)
    if not isinstance(context, dict) or not context.get("log_path"):
        return None
    normalized = normalize_token_usage(usage)
    event = {
        "schema_version": LLM_USAGE_SCHEMA_VERSION,
        "event_type": "llm_usage",
        "request_id": new_id("llmusage"),
        "recorded_at": now_iso(),
        "started_at": started_at,
        "duration_ms": max(0, int(duration_ms)),
        "cycle_id": str(context.get("cycle_id") or ""),
        "run_id": context.get("run_id"),
        "stage": str(context.get("stage") or ""),
        "role": str(context.get("role") or ""),
        "usage_category": str(context.get("usage_category") or ""),
        "provider": str(getattr(provider, "name", "unknown") or "unknown"),
        "model": model,
        "reasoning_effort": getattr(provider, "reasoning_effort", None),
        "status": status,
        "usage_reported": normalized is not None,
        "input_tokens": int((normalized or {}).get("input_tokens") or 0),
        "cached_input_tokens": int(
            (normalized or {}).get("cached_input_tokens") or 0
        ),
        "output_tokens": int((normalized or {}).get("output_tokens") or 0),
        "reasoning_output_tokens": int(
            (normalized or {}).get("reasoning_output_tokens") or 0
        ),
        "total_tokens": int((normalized or {}).get("total_tokens") or 0),
    }
    event["uncached_input_tokens"] = max(
        0, event["input_tokens"] - event["cached_input_tokens"]
    )
    if error_type:
        event["error_type"] = error_type
    if session_id:
        event["session_id"] = session_id
    if rate_limits:
        event["rate_limits"] = rate_limits
    append_usage_event(Path(str(context["log_path"])), event)
    return event


def append_usage_event(path: Path, event: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n"
        with _USAGE_WRITE_LOCK, path.open("a", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            handle.write(line)
            handle.flush()
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except (OSError, TypeError, ValueError):
        # Usage telemetry must never break the knowledge pipeline.
        return


def normalize_token_usage(value: dict[str, Any] | None) -> dict[str, int] | None:
    if not isinstance(value, dict):
        return None
    try:
        input_tokens = int(value.get("input_tokens") or 0)
        cached_input_tokens = int(value.get("cached_input_tokens") or 0)
        output_tokens = int(value.get("output_tokens") or 0)
        reasoning_output_tokens = int(value.get("reasoning_output_tokens") or 0)
        total_tokens = int(value.get("total_tokens") or 0)
    except (TypeError, ValueError):
        return None
    if total_tokens <= 0:
        total_tokens = input_tokens + output_tokens
    if not any(
        (input_tokens, cached_input_tokens, output_tokens, reasoning_output_tokens)
    ):
        return None
    return {
        "input_tokens": max(0, input_tokens),
        "cached_input_tokens": max(0, cached_input_tokens),
        "output_tokens": max(0, output_tokens),
        "reasoning_output_tokens": max(0, reasoning_output_tokens),
        "total_tokens": max(0, total_tokens),
    }


def openai_response_usage(response: dict[str, Any] | None) -> dict[str, int] | None:
    usage = response.get("usage") if isinstance(response, dict) else None
    if not isinstance(usage, dict):
        return None
    prompt_details = usage.get("prompt_tokens_details")
    completion_details = usage.get("completion_tokens_details")
    return normalize_token_usage(
        {
            "input_tokens": usage.get("prompt_tokens"),
            "cached_input_tokens": (
                prompt_details.get("cached_tokens")
                if isinstance(prompt_details, dict)
                else 0
            ),
            "output_tokens": usage.get("completion_tokens"),
            "reasoning_output_tokens": (
                completion_details.get("reasoning_tokens")
                if isinstance(completion_details, dict)
                else 0
            ),
            "total_tokens": usage.get("total_tokens"),
        }
    )


def anthropic_response_usage(
    response: dict[str, Any] | None,
) -> dict[str, int] | None:
    usage = response.get("usage") if isinstance(response, dict) else None
    if not isinstance(usage, dict):
        return None
    input_tokens = safe_token_count(usage.get("input_tokens"))
    output_tokens = safe_token_count(usage.get("output_tokens"))
    return normalize_token_usage(
        {
            "input_tokens": input_tokens,
            "cached_input_tokens": usage.get("cache_read_input_tokens"),
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        }
    )


def ollama_response_usage(response: dict[str, Any] | None) -> dict[str, int] | None:
    if not isinstance(response, dict):
        return None
    input_tokens = safe_token_count(response.get("prompt_eval_count"))
    output_tokens = safe_token_count(response.get("eval_count"))
    return normalize_token_usage(
        {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        }
    )


def safe_token_count(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def codex_jsonl_usage(
    output: str,
) -> tuple[dict[str, int] | None, dict[str, Any]]:
    usages: list[dict[str, int]] = []
    metadata: dict[str, Any] = {}
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        session_id = first_string(
            event.get("thread_id"),
            event.get("session_id"),
            nested_value(event, "payload", "session_id"),
        )
        if session_id:
            metadata["session_id"] = session_id
        usage = codex_event_usage(event)
        if usage is not None:
            usages.append(usage)
        rate_limits = codex_event_rate_limits(event)
        if rate_limits:
            metadata["rate_limits"] = rate_limits
    return (usages[-1] if usages else None), metadata


def codex_event_usage(event: dict[str, Any]) -> dict[str, int] | None:
    candidates = [
        event.get("usage"),
        nested_value(event, "payload", "usage"),
        nested_value(event, "info", "last_token_usage"),
        nested_value(event, "payload", "info", "last_token_usage"),
        nested_value(event, "info", "total_token_usage"),
        nested_value(event, "payload", "info", "total_token_usage"),
    ]
    for candidate in candidates:
        normalized = normalize_token_usage(candidate if isinstance(candidate, dict) else None)
        if normalized is not None:
            return normalized
    return None


def codex_event_rate_limits(event: dict[str, Any]) -> dict[str, Any] | None:
    for candidate in (
        event.get("rate_limits"),
        nested_value(event, "payload", "rate_limits"),
    ):
        if isinstance(candidate, dict):
            return candidate
    return None


def nested_value(value: dict[str, Any], *keys: str) -> Any:
    current: Any = value
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def first_string(*values: Any) -> str | None:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def llm_usage_summary(
    paths: BrainPaths,
    *,
    cycle_id: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    path = llm_usage_log_path(paths)
    events = read_usage_events(path)
    if cycle_id:
        events = [
            event
            for event in events
            if event.get("cycle_id") == cycle_id or event.get("run_id") == cycle_id
        ]
    grouped: dict[str, list[dict[str, Any]]] = {}
    for event in events:
        key = str(event.get("cycle_id") or event.get("run_id") or "uncategorized")
        grouped.setdefault(key, []).append(event)
    cycles = [usage_cycle_row(key, rows) for key, rows in grouped.items()]
    cycles.sort(key=lambda item: str(item.get("finished_at") or ""), reverse=True)
    available_cycle_count = len(cycles)
    cycles = cycles[: max(1, int(limit))]
    return {
        "generated_at": now_iso(),
        "log_path": str(path),
        "cycle_count": len(cycles),
        "available_cycle_count": available_cycle_count,
        "totals": usage_totals(cycles),
        "roles": usage_roles_totals(cycles),
        "cycles": cycles,
    }


def read_usage_events(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    buffered: deque[dict[str, Any]] = deque(maxlen=MAX_USAGE_EVENTS_TO_SUMMARIZE)
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(event, dict) and event.get("event_type") == "llm_usage":
                    buffered.append(event)
    except OSError:
        return []
    return list(buffered)


def usage_cycle_row(cycle_id: str, events: list[dict[str, Any]]) -> dict[str, Any]:
    roles: dict[str, list[dict[str, Any]]] = {}
    for event in events:
        key = str(event.get("usage_category") or event.get("role") or "unknown")
        roles.setdefault(key, []).append(event)
    role_rows = [usage_role_row(role, rows) for role, rows in sorted(roles.items())]
    return {
        "cycle_id": cycle_id,
        "run_id": first_string(*(event.get("run_id") for event in events)),
        "started_at": min(str(event.get("started_at") or "") for event in events),
        "finished_at": max(str(event.get("recorded_at") or "") for event in events),
        "request_count": len(events),
        "unreported_request_count": sum(
            1 for event in events if not event.get("usage_reported")
        ),
        "input_tokens": sum(int(event.get("input_tokens") or 0) for event in events),
        "cached_input_tokens": sum(
            int(event.get("cached_input_tokens") or 0) for event in events
        ),
        "uncached_input_tokens": sum(
            int(event.get("uncached_input_tokens") or 0) for event in events
        ),
        "output_tokens": sum(int(event.get("output_tokens") or 0) for event in events),
        "reasoning_output_tokens": sum(
            int(event.get("reasoning_output_tokens") or 0) for event in events
        ),
        "total_tokens": sum(int(event.get("total_tokens") or 0) for event in events),
        "roles": role_rows,
    }


def usage_role_row(role: str, events: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "role": role,
        "source_roles": sorted({str(event.get("role") or "") for event in events}),
        "request_count": len(events),
        "unreported_request_count": sum(
            1 for event in events if not event.get("usage_reported")
        ),
        "models": sorted(
            {
                f"{event.get('provider')}:{event.get('model')}:{event.get('reasoning_effort') or 'default'}"
                for event in events
            }
        ),
        "input_tokens": sum(int(event.get("input_tokens") or 0) for event in events),
        "cached_input_tokens": sum(
            int(event.get("cached_input_tokens") or 0) for event in events
        ),
        "uncached_input_tokens": sum(
            int(event.get("uncached_input_tokens") or 0) for event in events
        ),
        "output_tokens": sum(int(event.get("output_tokens") or 0) for event in events),
        "reasoning_output_tokens": sum(
            int(event.get("reasoning_output_tokens") or 0) for event in events
        ),
        "total_tokens": sum(int(event.get("total_tokens") or 0) for event in events),
    }


def usage_totals(cycles: list[dict[str, Any]]) -> dict[str, Any]:
    if not cycles:
        return {
            "started_at": None,
            "finished_at": None,
            "request_count": 0,
            "unreported_request_count": 0,
            "input_tokens": 0,
            "cached_input_tokens": 0,
            "uncached_input_tokens": 0,
            "output_tokens": 0,
            "reasoning_output_tokens": 0,
            "total_tokens": 0,
        }
    fields = (
        "request_count",
        "unreported_request_count",
        "input_tokens",
        "cached_input_tokens",
        "uncached_input_tokens",
        "output_tokens",
        "reasoning_output_tokens",
        "total_tokens",
    )
    return {
        "started_at": min(str(cycle.get("started_at") or "") for cycle in cycles),
        "finished_at": max(
            str(cycle.get("finished_at") or "") for cycle in cycles
        ),
        **{
            field: sum(safe_token_count(cycle.get(field)) for cycle in cycles)
            for field in fields
        },
    }


def usage_roles_totals(cycles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for cycle in cycles:
        for role in cycle.get("roles") or []:
            if not isinstance(role, dict):
                continue
            grouped.setdefault(str(role.get("role") or "unknown"), []).append(role)
    rows: list[dict[str, Any]] = []
    for role_name, role_rows in sorted(grouped.items()):
        totals = usage_totals(role_rows)
        totals.pop("started_at", None)
        totals.pop("finished_at", None)
        rows.append(
            {
                "role": role_name,
                "source_roles": sorted(
                    {
                        str(source_role)
                        for row in role_rows
                        for source_role in row.get("source_roles") or []
                    }
                ),
                "models": sorted(
                    {
                        str(model)
                        for row in role_rows
                        for model in row.get("models") or []
                    }
                ),
                **totals,
            }
        )
    return rows
