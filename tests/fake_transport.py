from __future__ import annotations

import json
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
    ) -> None:
        self.remote_node_id = remote_node_id
        self.remote_role = remote_role
        self.remote_home = remote_home or Path("/tmp/secondary-brain")
        self.remote_brain = remote_brain
        self.outbox_probe = outbox_probe
        self.local_rsync = local_rsync
        self.remote_rsync = remote_rsync
        self.ssh = ssh
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
        return SubprocessResult(1, "", f"unexpected command: {command}")

    def rsync(self, args: list[str]) -> SubprocessResult:
        self.rsync_commands.append(args)
        return SubprocessResult(0 if self.local_rsync else 1, "rsync version\n", "missing rsync")
