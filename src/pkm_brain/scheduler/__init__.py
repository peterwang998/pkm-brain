from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol


@dataclass(frozen=True)
class ScheduledJob:
    label: str
    command: str
    interval: int
    brain_home: Path
    repo_path: Path
    stdout_path: Path
    stderr_path: Path


@dataclass(frozen=True)
class JobStatus:
    label: str
    path: str
    plist_exists: bool
    loaded: bool
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


class Scheduler(Protocol):
    def install(self, job: ScheduledJob, dry_run: bool = False) -> dict[str, Any]:
        ...

    def uninstall(self, label: str) -> dict[str, Any]:
        ...

    def status(self, label: str | None = None) -> list[JobStatus]:
        ...
