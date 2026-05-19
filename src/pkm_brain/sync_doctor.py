from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .paths import BrainPaths
from .sync_config import SyncConfig, load_sync_config


@dataclass(frozen=True)
class SyncDoctorCheck:
    name: str
    status: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return {"name": self.name, "status": self.status, "message": self.message}


@dataclass(frozen=True)
class SyncDoctorResult:
    brain_home: str
    role: str | None
    node_id: str | None
    ready: bool
    checks: list[SyncDoctorCheck]

    def as_dict(self) -> dict[str, Any]:
        return {
            "brain_home": self.brain_home,
            "role": self.role,
            "node_id": self.node_id,
            "ready": self.ready,
            "checks": [check.as_dict() for check in self.checks],
        }


def run_sync_doctor(paths: BrainPaths) -> SyncDoctorResult:
    checks: list[SyncDoctorCheck] = []
    config: SyncConfig | None = None
    role: str | None = None
    node_id: str | None = None

    if paths.sync_config_file.exists():
        checks.append(ok("sync_config_exists", f"found {paths.sync_config_file}"))
        try:
            config = load_sync_config(paths)
            role = config.role
            node_id = config.node_id
            checks.append(ok("sync_config_valid", "sync.yaml parsed successfully"))
        except Exception as exc:
            checks.append(fail("sync_config_valid", str(exc)))
    else:
        checks.append(fail("sync_config_exists", f"missing {paths.sync_config_file}"))

    if config:
        checks.append(ok("node_id", config.node_id))
        checks.append(ok("role", config.role))
        if config.brain_home.expanduser().resolve() == paths.home:
            checks.append(ok("brain_home", str(config.brain_home)))
        else:
            checks.append(fail("brain_home", f"config has {config.brain_home}, actual home is {paths.home}"))
        if config.role == "primary":
            checks.extend(check_required_dirs([paths.inbox / "external"]))
            checks.append(ok("primary_config", f"{len(config.primary.peers if config.primary else [])} peer(s) configured"))
        elif config.secondary:
            checks.append(ok("secondary_primary", config.secondary.primary_node_id))
            if config.secondary.outbox_path:
                checks.extend(check_required_dirs([config.secondary.outbox_path]))
                if config.node_id in str(config.secondary.outbox_path):
                    checks.append(ok("secondary_outbox_path", str(config.secondary.outbox_path)))
                else:
                    checks.append(fail("secondary_outbox_path", "outbox path must include node_id"))
    else:
        checks.append(fail("node_id", "not available"))
        checks.append(fail("role", "not available"))

    checks.extend(check_required_dirs(paths.directories()))
    for local_only in [paths.db_dir, paths.indexes, paths.logs]:
        checks.append(ok("local_only_path", f"{local_only.name}/ is local-only"))

    ready = all(check.status == "ok" for check in checks)
    return SyncDoctorResult(str(paths.home), role, node_id, ready, checks)


def check_required_dirs(paths: list[Path]) -> list[SyncDoctorCheck]:
    checks: list[SyncDoctorCheck] = []
    for path in paths:
        try:
            path.mkdir(parents=True, exist_ok=True)
            checks.append(ok("required_dir", str(path)))
        except OSError as exc:
            checks.append(fail("required_dir", f"{path}: {exc}"))
    return checks


def ok(name: str, message: str) -> SyncDoctorCheck:
    return SyncDoctorCheck(name=name, status="ok", message=message)


def fail(name: str, message: str) -> SyncDoctorCheck:
    return SyncDoctorCheck(name=name, status="fail", message=message)
