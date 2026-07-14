from __future__ import annotations

import threading
from collections.abc import Mapping, Sequence
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .operational_db import OperationalStoreUnavailableError
from .operational_service import OperationalService
from .operational_shadow import list_shadow_runs
from .paths import BrainPaths
from .shadow_setup import (
    ensure_default_operations_policy,
    validate_operations_policy_auth_binding,
)
from .shadow_trial import ShadowTrialRunner
from .util import now_iso


class ShadowTrialController:
    """Start one background trial at a time and expose bounded polling state."""

    def __init__(self, paths: BrainPaths, operational_service: OperationalService) -> None:
        self.paths = paths
        self.operational_service = operational_service
        self._lock = threading.RLock()
        self._state: dict[str, Any] | None = None
        self._thread: threading.Thread | None = None

    def start(
        self,
        *,
        timezone_name: str,
        sources: Sequence[str],
    ) -> dict[str, Any]:
        normalized_sources = _validated_sources(sources)
        try:
            ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"unknown local timezone: {timezone_name}") from exc
        policy = ensure_default_operations_policy(
            self.paths,
            timezone_name=timezone_name,
            sources=normalized_sources,
        )
        # Existing policies stay immutable, but authorization can change later.
        # Re-bind immediately before every live run so `users/me` and `primary`
        # cannot silently target a different signed-in Google account.
        validate_operations_policy_auth_binding(
            self.paths,
            policy,
            sources=normalized_sources,
        )
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                assert self._state is not None
                return dict(self._state)
            started_at = now_iso()
            self.operational_service.initialize()
            self.operational_service.interrupt_running_shadow_runs(
                interrupted_at=started_at
            )
            self._state = {
                "schema_version": 1,
                "status": "accepted",
                "message": "Read-only Shadow run accepted.",
                "run_id": None,
                "started_at": started_at,
                "finished_at": None,
                "sources": list(normalized_sources),
                "coverage": {},
                "usage": {},
                "counts": {},
            }
            self._thread = threading.Thread(
                target=self._run,
                kwargs={
                    "policy": policy,
                    "sources": normalized_sources,
                    "started_at": started_at,
                },
                name="brain-chief-of-staff-shadow",
                daemon=True,
            )
            self._thread.start()
            return dict(self._state)

    def status(self) -> dict[str, Any]:
        with self._lock:
            if self._state is not None:
                return dict(self._state)
        try:
            runs = list_shadow_runs(self.paths.ops_sqlite_path, limit=1)
        except OperationalStoreUnavailableError:
            runs = []
        if not runs:
            return {
                "schema_version": 1,
                "status": "idle",
                "message": "Authorize Calendar and Gmail, then run Shadow.",
                "run_id": None,
                "started_at": None,
                "finished_at": None,
                "sources": [],
                "coverage": {},
                "usage": {},
                "counts": {},
            }
        run = runs[0]
        return _run_status_payload(run)

    def _run(self, *, policy: Any, sources: tuple[str, ...], started_at: str) -> None:
        with self._lock:
            assert self._state is not None
            self._state = {
                **self._state,
                "status": "running",
                "message": "Reading Calendar and Gmail in shadow mode…",
            }
        try:
            result = ShadowTrialRunner(
                self.paths,
                self.operational_service,
            ).run_live(sources=sources, policy=policy)
            payload = _run_status_payload(result.run)
            payload["message"] = _completion_message(payload)
        except Exception as exc:
            payload = {
                "schema_version": 1,
                "status": "failed",
                "message": f"Shadow run failed: {exc}",
                "run_id": None,
                "started_at": started_at,
                "finished_at": now_iso(),
                "sources": list(sources),
                "coverage": {},
                "usage": {},
                "counts": {},
            }
        with self._lock:
            self._state = payload


def shadow_run_start_payload(
    controller: ShadowTrialController,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    timezone_name = str(payload.get("timezone_name") or "").strip()
    if not timezone_name:
        raise ValueError("timezone_name is required")
    raw_sources = payload.get("sources")
    if not isinstance(raw_sources, list):
        raise ValueError("sources must be a list")
    return controller.start(
        timezone_name=timezone_name,
        sources=[str(value) for value in raw_sources],
    )


def _validated_sources(values: Sequence[str]) -> tuple[str, ...]:
    output: list[str] = []
    for value in values:
        source = str(value).strip().casefold()
        if source not in {"calendar", "gmail"}:
            raise ValueError(f"unsupported Shadow source: {source or '(missing)'}")
        if source not in output:
            output.append(source)
    if set(output) != {"calendar", "gmail"}:
        raise ValueError("the initial Shadow trial requires Calendar and Gmail")
    return tuple(output)


def _run_status_payload(run: Mapping[str, Any]) -> dict[str, Any]:
    status = str(run.get("status") or "failed")
    message = str(run.get("error") or "Shadow run is complete.")
    if status == "running":
        status = "failed"
        message = (
            "The previous Shadow run was interrupted before it could finish. "
            "You can run Shadow again safely."
        )
    if status == "stopped":
        status = "failed"
    return {
        "schema_version": 1,
        "status": status,
        "message": message,
        "run_id": run.get("id"),
        "started_at": run.get("started_at"),
        "finished_at": run.get("finished_at"),
        "sources": list(run.get("requested_sources") or []),
        "coverage": dict(run.get("coverage") or {}),
        "usage": dict(run.get("usage") or {}),
        "counts": dict(run.get("counts") or {}),
    }


def _completion_message(payload: Mapping[str, Any]) -> str:
    status = str(payload.get("status") or "failed")
    counts = payload.get("counts") if isinstance(payload.get("counts"), Mapping) else {}
    item_count = int(counts.get("calendar_items") or 0) + int(
        counts.get("gmail_items") or 0
    )
    if status == "complete":
        return f"Shadow run complete with {item_count} operational item(s)."
    if status == "partial":
        return (
            f"Shadow run completed partially with {item_count} item(s); "
            "coverage details remain visible."
        )
    return str(payload.get("message") or "Shadow run failed.")
