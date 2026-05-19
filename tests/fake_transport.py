from __future__ import annotations

import fnmatch
import json
import shutil
from pathlib import Path

from pkm_brain.sync_connection import SubprocessResult


class FakeTransport:
    def __init__(
        self,
        remote_node_id: str = "secondary",
        remote_role: str = "secondary",
        remote_home: Path | None = None,
        remote_brain: bool = True,
        outbox_probe: bool = True,
        local_rsync: bool = True,
        remote_rsync: bool = True,
        ssh: bool = True,
        remote_ingest: bool = True,
        fail_rsync_call: int | None = None,
    ) -> None:
        self.remote_node_id = remote_node_id
        self.remote_role = remote_role
        self.remote_home = remote_home or Path("/tmp/secondary-brain")
        self.remote_brain = remote_brain
        self.outbox_probe = outbox_probe
        self.local_rsync = local_rsync
        self.remote_rsync = remote_rsync
        self.ssh = ssh
        self.remote_ingest = remote_ingest
        self.fail_rsync_call = fail_rsync_call
        self.commands: list[list[str]] = []
        self.rsync_commands: list[list[str]] = []

    def run(self, host: str, argv: list[str]) -> SubprocessResult:
        self.commands.append(argv)
        command = argv[-1]
        if not self.ssh:
            return SubprocessResult(255, "", "ssh failed")
        if command == "true":
            return SubprocessResult(0, "", "")
        if command == "command -v brain":
            return SubprocessResult(0 if self.remote_brain else 1, "/usr/local/bin/brain\n", "missing brain")
        if command.startswith("brain sync doctor --json"):
            return SubprocessResult(
                0,
                json.dumps(
                    {
                        "brain_home": str(self.remote_home),
                        "role": self.remote_role,
                        "node_id": self.remote_node_id,
                        "ready": self.remote_role == "secondary",
                        "checks": [],
                    }
                ),
                "",
            )
        if "printf ok" in command:
            return SubprocessResult(0 if self.outbox_probe else 1, "", "outbox probe failed")
        if command == "rsync --version":
            return SubprocessResult(0 if self.remote_rsync else 1, "rsync version\n", "missing rsync")
        if command.startswith("brain ingest --home"):
            return SubprocessResult(0 if self.remote_ingest else 1, '{"changed":0}\n', "remote ingest failed")
        return SubprocessResult(1, "", f"unexpected command: {command}")

    def rsync(self, args: list[str]) -> SubprocessResult:
        self.rsync_commands.append(args)
        return SubprocessResult(0 if self.local_rsync else 1, "rsync version\n", "missing rsync")


class LocalRsyncTransport(FakeTransport):
    def __init__(self, *args, fail_rsync: bool = False, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.fail_rsync = fail_rsync

    def rsync(self, args: list[str]) -> SubprocessResult:
        self.rsync_commands.append(args)
        if args == ["rsync", "--version"]:
            return SubprocessResult(0 if self.local_rsync else 1, "rsync version\n", "missing rsync")
        rsync_call_number = len([command for command in self.rsync_commands if command != ["rsync", "--version"]])
        if self.fail_rsync or self.fail_rsync_call == rsync_call_number:
            return SubprocessResult(23, "", "simulated rsync failure")
        excludes = excludes_from_rsync_args(args)
        source = local_path_from_rsync_endpoint(args[-2])
        target = local_path_from_rsync_endpoint(args[-1])
        copy_tree(source, target, excludes)
        return SubprocessResult(0, "", "")


def excludes_from_rsync_args(args: list[str]) -> list[str]:
    excludes: list[str] = []
    for index, value in enumerate(args):
        if value == "--exclude" and index + 1 < len(args):
            excludes.append(args[index + 1])
    return excludes


def local_path_from_rsync_endpoint(value: str) -> Path:
    if ":" in value and not value.startswith("/"):
        value = value.split(":", 1)[1]
    return Path(value.rstrip("/"))


def copy_tree(source: Path, target: Path, excludes: list[str]) -> None:
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True, exist_ok=True)
    if not source.exists():
        return
    for path in sorted(source.rglob("*")):
        if path.is_dir():
            continue
        relative = path.relative_to(source)
        if should_exclude(relative, excludes):
            continue
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination)


def should_exclude(relative: Path, excludes: list[str]) -> bool:
    rel = relative.as_posix()
    for pattern in excludes:
        if pattern.endswith("/") and (rel.startswith(pattern) or pattern.rstrip("/") in relative.parts):
            return True
        if fnmatch.fnmatch(relative.name, pattern) or fnmatch.fnmatch(rel, pattern):
            return True
    return False
