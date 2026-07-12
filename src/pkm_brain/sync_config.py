from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .paths import BrainPaths


VALID_ROLES = {"primary", "secondary"}
FORBIDDEN_MIRROR_PARTS = {"db", "indexes", "logs"}


@dataclass(frozen=True)
class PeerConfig:
    node_id: str
    role: str = "secondary"
    host: str | None = None
    user: str | None = None
    brain_home: Path | None = None
    outbox_path: Path | None = None
    transport: str = "ssh"
    trust: str | None = None
    identity_path: Path | None = None
    host_key_fingerprint: str | None = None
    mirror_paths: list[str] = field(default_factory=list)
    cadence_s: int | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PeerConfig":
        node_id = required_string(data, "node_id")
        role = str(data.get("role") or "secondary")
        if role not in VALID_ROLES:
            raise ValueError(f"unknown peer role for {node_id}: {role}")
        mirror_paths = [str(path) for path in data.get("mirror_paths") or []]
        validate_mirror_paths(mirror_paths)
        return cls(
            node_id=node_id,
            role=role,
            host=optional_string(data, "host"),
            user=optional_string(data, "user"),
            brain_home=optional_path(data, "brain_home"),
            outbox_path=optional_path(data, "outbox_path"),
            transport=str(data.get("transport") or "ssh"),
            trust=optional_string(data, "trust"),
            identity_path=optional_path(data, "identity_path"),
            host_key_fingerprint=optional_string(data, "host_key_fingerprint"),
            mirror_paths=mirror_paths,
            cadence_s=optional_positive_int(data, "cadence_s"),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "role": self.role,
            "host": self.host,
            "user": self.user,
            "brain_home": str(self.brain_home) if self.brain_home else None,
            "outbox_path": str(self.outbox_path) if self.outbox_path else None,
            "transport": self.transport,
            "trust": self.trust,
            "identity_path": str(self.identity_path) if self.identity_path else None,
            "host_key_fingerprint": self.host_key_fingerprint,
            "mirror_paths": list(self.mirror_paths),
            "cadence_s": self.cadence_s,
        }


@dataclass(frozen=True)
class PrimaryConfig:
    peers: list[PeerConfig] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PrimaryConfig":
        peers = [PeerConfig.from_dict(peer) for peer in data.get("peers") or []]
        peer_ids = [peer.node_id for peer in peers]
        duplicate_ids = sorted({node_id for node_id in peer_ids if peer_ids.count(node_id) > 1})
        if duplicate_ids:
            raise ValueError(f"duplicate peer node_id: {', '.join(duplicate_ids)}")
        return cls(peers=peers)

    def as_dict(self) -> dict[str, Any]:
        return {"peers": [peer.as_dict() for peer in self.peers]}


@dataclass(frozen=True)
class SecondaryConfig:
    primary_node_id: str
    primary_expected_user: str | None = None
    outbox_enabled: bool = True
    outbox_path: Path | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any], node_id: str) -> "SecondaryConfig":
        primary = data.get("primary") or {}
        if not isinstance(primary, dict):
            raise ValueError("secondary primary config must be a mapping")
        outbox = data.get("outbox") or {}
        if not isinstance(outbox, dict):
            raise ValueError("secondary outbox config must be a mapping")
        primary_node_id = required_string(primary, "node_id", label="primary.node_id")
        outbox_path = optional_path(outbox, "path")
        if outbox_path is None:
            raise ValueError("secondary outbox.path is required")
        if node_id not in str(outbox_path):
            raise ValueError("secondary outbox.path must include the secondary node_id")
        return cls(
            primary_node_id=primary_node_id,
            primary_expected_user=optional_string(primary, "expected_user"),
            outbox_enabled=bool(outbox.get("enabled", True)),
            outbox_path=outbox_path,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "primary": {
                "node_id": self.primary_node_id,
                "expected_user": self.primary_expected_user,
            },
            "outbox": {
                "enabled": self.outbox_enabled,
                "path": str(self.outbox_path) if self.outbox_path else None,
            },
        }


@dataclass(frozen=True)
class SyncConfig:
    node_id: str
    role: str
    brain_home: Path
    primary: PrimaryConfig | None = None
    secondary: SecondaryConfig | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any], default_home: Path) -> "SyncConfig":
        if not isinstance(data, dict):
            raise ValueError("sync config must be a mapping")
        node_id = required_string(data, "node_id")
        role = required_string(data, "role")
        if role not in VALID_ROLES:
            raise ValueError(f"unknown role: {role}")
        brain_home = optional_path(data, "brain_home") or default_home
        if role == "primary":
            primary = PrimaryConfig.from_dict(data)
            return cls(node_id=node_id, role=role, brain_home=brain_home, primary=primary, raw=data)
        secondary = SecondaryConfig.from_dict(data, node_id)
        return cls(node_id=node_id, role=role, brain_home=brain_home, secondary=secondary, raw=data)

    def as_dict(self) -> dict[str, Any]:
        payload = {
            "node_id": self.node_id,
            "role": self.role,
            "brain_home": str(self.brain_home),
        }
        if self.primary:
            payload.update(self.primary.as_dict())
        if self.secondary:
            payload.update(self.secondary.as_dict())
        return payload


def load_sync_config(home: str | Path | BrainPaths) -> SyncConfig:
    paths = home if isinstance(home, BrainPaths) else BrainPaths.from_value(home)
    if not paths.sync_config_file.exists():
        raise FileNotFoundError(paths.sync_config_file)
    data = yaml.safe_load(paths.sync_config_file.read_text(encoding="utf-8")) or {}
    return SyncConfig.from_dict(data, paths.home)


def write_sync_config(home: str | Path | BrainPaths, config: SyncConfig) -> Path:
    paths = home if isinstance(home, BrainPaths) else BrainPaths.from_value(home)
    paths.config.mkdir(parents=True, exist_ok=True)
    payload = yaml.safe_dump(config.as_dict(), sort_keys=False, allow_unicode=False)
    paths.sync_config_file.write_text(payload, encoding="utf-8")
    return paths.sync_config_file


def required_string(data: dict[str, Any], key: str, label: str | None = None) -> str:
    value = data.get(key)
    if value is None or str(value).strip() == "":
        raise ValueError(f"{label or key} is required")
    return str(value).strip()


def optional_string(data: dict[str, Any], key: str) -> str | None:
    value = data.get(key)
    if value is None or str(value).strip() == "":
        return None
    return str(value).strip()


def optional_path(data: dict[str, Any], key: str) -> Path | None:
    value = optional_string(data, key)
    return Path(value).expanduser() if value else None


def optional_positive_int(data: dict[str, Any], key: str) -> int | None:
    value = data.get(key)
    if value is None or str(value).strip() == "":
        return None
    parsed = int(value)
    if parsed <= 0:
        raise ValueError(f"{key} must be positive")
    return parsed


def validate_mirror_paths(mirror_paths: list[str]) -> None:
    for mirror_path in mirror_paths:
        if mirror_path_forbidden(mirror_path):
            raise ValueError(f"mirror path is local-only and cannot be synced: {mirror_path}")


def mirror_path_forbidden(mirror_path: str) -> bool:
    normalized = mirror_path.replace("\\", "/").strip()
    parts = {part for part in normalized.split("/") if part}
    if parts.intersection(FORBIDDEN_MIRROR_PARTS):
        return True
    return any(".sqlite" in part for part in parts)
