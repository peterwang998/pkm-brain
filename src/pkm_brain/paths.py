from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


DEFAULT_BRAIN_HOME = Path("~/brain").expanduser()


@dataclass(frozen=True)
class BrainPaths:
    home: Path

    @classmethod
    def from_value(cls, value: str | Path | None = None) -> "BrainPaths":
        raw = value or os.environ.get("BRAIN_HOME") or DEFAULT_BRAIN_HOME
        return cls(Path(raw).expanduser().resolve())

    @property
    def inbox(self) -> Path:
        return self.home / "inbox"

    @property
    def raw(self) -> Path:
        return self.home / "raw"

    @property
    def wiki(self) -> Path:
        return self.home / "wiki"

    @property
    def memory(self) -> Path:
        return self.home / "memory"

    @property
    def indexes(self) -> Path:
        return self.home / "indexes"

    @property
    def db_dir(self) -> Path:
        return self.home / "db"

    @property
    def logs(self) -> Path:
        return self.home / "logs"

    @property
    def config(self) -> Path:
        return self.home / "config"

    @property
    def evals(self) -> Path:
        return self.home / "evals"

    @property
    def sqlite_path(self) -> Path:
        return self.db_dir / "brain.sqlite"

    @property
    def lancedb_path(self) -> Path:
        return self.indexes / "lancedb"

    @property
    def config_file(self) -> Path:
        return self.config / "config.yaml"

    @property
    def golden_queries_file(self) -> Path:
        return self.evals / "golden_queries.yaml"

    def directories(self) -> list[Path]:
        return [
            self.home,
            self.inbox,
            self.raw,
            self.wiki,
            self.memory,
            self.indexes,
            self.db_dir,
            self.logs,
            self.config,
            self.evals,
        ]
