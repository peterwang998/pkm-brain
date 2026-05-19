from __future__ import annotations

from typing import Any

from . import JobStatus, ScheduledJob


MESSAGE = "Linux scheduler not yet implemented; use on-demand commands or launchd on macOS"


class Scheduler:
    def install(self, job: ScheduledJob, dry_run: bool = False) -> dict[str, Any]:
        raise NotImplementedError(MESSAGE)

    def uninstall(self, label: str) -> dict[str, Any]:
        raise NotImplementedError(MESSAGE)

    def status(self, label: str | None = None) -> list[JobStatus]:
        raise NotImplementedError(MESSAGE)
