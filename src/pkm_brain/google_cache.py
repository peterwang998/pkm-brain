from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .paths import BrainPaths


RAW_RETENTION_DAYS = 7
NORMALIZED_RETENTION_DAYS = 30
PRIVATE_DIRECTORY_MODE = 0o700
PRIVATE_FILE_MODE = 0o600
_CONNECTOR_ID = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")


class GoogleCacheSecurityError(RuntimeError):
    pass


@dataclass(frozen=True)
class CacheRetentionResult:
    removed_files: int
    removed_bytes: int
    retained_files: int
    errors: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "removed_files": self.removed_files,
            "removed_bytes": self.removed_bytes,
            "retained_files": self.retained_files,
            "errors": list(self.errors),
        }


class GoogleEvidenceCache:
    """Private, disposable evidence cache with lane-specific retention."""

    def __init__(
        self,
        root: Path,
        *,
        raw_retention_days: int = RAW_RETENTION_DAYS,
        normalized_retention_days: int = NORMALIZED_RETENTION_DAYS,
    ) -> None:
        if raw_retention_days <= 0 or normalized_retention_days <= 0:
            raise ValueError("Google cache retention days must be positive")
        self.root = root.expanduser().absolute()
        self.raw_root = self.root / "raw"
        self.normalized_root = self.root / "normalized"
        self.raw_retention = timedelta(days=raw_retention_days)
        self.normalized_retention = timedelta(days=normalized_retention_days)
        for path in (self.root, self.raw_root, self.normalized_root):
            _ensure_private_directory(path)

    @classmethod
    def for_paths(cls, paths: BrainPaths) -> "GoogleEvidenceCache":
        return cls(paths.home / "cache" / "google-evidence")

    def write_raw(
        self,
        connector_id: str,
        object_key: str,
        payload: Any,
        *,
        cached_at: datetime | None = None,
    ) -> Path:
        return self._write(
            "raw",
            connector_id,
            object_key,
            payload,
            cached_at=cached_at,
        )

    def write_normalized(
        self,
        connector_id: str,
        object_key: str,
        payload: Any,
        *,
        cached_at: datetime | None = None,
    ) -> Path:
        return self._write(
            "normalized",
            connector_id,
            object_key,
            payload,
            cached_at=cached_at,
        )

    def read_raw(self, connector_id: str, object_key: str) -> Any | None:
        return self._read("raw", connector_id, object_key)

    def read_normalized(self, connector_id: str, object_key: str) -> Any | None:
        return self._read("normalized", connector_id, object_key)

    def prune(self, *, now: datetime | None = None) -> CacheRetentionResult:
        current = _aware_utc(now or datetime.now(timezone.utc))
        removed_files = 0
        removed_bytes = 0
        retained_files = 0
        errors: list[str] = []
        for lane, root, retention in (
            ("raw", self.raw_root, self.raw_retention),
            ("normalized", self.normalized_root, self.normalized_retention),
        ):
            _assert_private_directory(root)
            for connector_dir in sorted(root.iterdir()):
                try:
                    _assert_private_directory(connector_dir)
                except GoogleCacheSecurityError as exc:
                    errors.append(str(exc))
                    continue
                for path in sorted(connector_dir.iterdir()):
                    try:
                        file_stat = _assert_private_file(path)
                        envelope = _read_json_file(path)
                        cached_at = _parse_cached_at(envelope)
                    except (GoogleCacheSecurityError, RuntimeError, ValueError) as exc:
                        errors.append(f"{lane}/{connector_dir.name}/{path.name}: {exc}")
                        continue
                    if current - cached_at <= retention:
                        retained_files += 1
                        continue
                    try:
                        path.unlink()
                    except OSError as exc:
                        errors.append(
                            f"{lane}/{connector_dir.name}/{path.name}: unable to remove: {exc}"
                        )
                        continue
                    removed_files += 1
                    removed_bytes += file_stat.st_size
        return CacheRetentionResult(
            removed_files=removed_files,
            removed_bytes=removed_bytes,
            retained_files=retained_files,
            errors=tuple(errors),
        )

    def _write(
        self,
        lane: str,
        connector_id: str,
        object_key: str,
        payload: Any,
        *,
        cached_at: datetime | None,
    ) -> Path:
        connector = _validated_connector_id(connector_id)
        normalized_key = object_key.strip()
        if not normalized_key or len(normalized_key) > 4_000:
            raise ValueError("Google cache object key must be 1-4000 characters")
        root = self.raw_root if lane == "raw" else self.normalized_root
        _assert_private_directory(root)
        connector_dir = root / connector
        _ensure_private_directory(connector_dir)
        digest = hashlib.sha256(normalized_key.encode("utf-8")).hexdigest()
        path = connector_dir / f"{digest}.json"
        envelope = {
            "schema_version": 1,
            "lane": lane,
            "connector_id": connector,
            "object_key": normalized_key,
            "cached_at": _aware_utc(
                cached_at or datetime.now(timezone.utc)
            ).replace(microsecond=0).isoformat(),
            "payload": payload,
        }
        _atomic_private_json(path, envelope)
        return path

    def _read(self, lane: str, connector_id: str, object_key: str) -> Any | None:
        connector = _validated_connector_id(connector_id)
        normalized_key = object_key.strip()
        if not normalized_key:
            raise ValueError("Google cache object key cannot be empty")
        root = self.raw_root if lane == "raw" else self.normalized_root
        connector_dir = root / connector
        digest = hashlib.sha256(normalized_key.encode("utf-8")).hexdigest()
        path = connector_dir / f"{digest}.json"
        if not path.exists() and not path.is_symlink():
            return None
        _assert_private_directory(connector_dir)
        _assert_private_file(path)
        envelope = _read_json_file(path)
        if (
            envelope.get("schema_version") != 1
            or envelope.get("lane") != lane
            or envelope.get("connector_id") != connector
            or envelope.get("object_key") != normalized_key
        ):
            raise RuntimeError("Google cache envelope identity does not match its path")
        return envelope.get("payload")


def _ensure_private_directory(path: Path) -> None:
    if path.is_symlink():
        raise GoogleCacheSecurityError(f"Google cache directory must not be a symlink: {path}")
    path.mkdir(parents=True, exist_ok=True, mode=PRIVATE_DIRECTORY_MODE)
    _assert_private_directory(path)
    os.chmod(path, PRIVATE_DIRECTORY_MODE)


def _assert_private_directory(path: Path) -> os.stat_result:
    try:
        value = os.stat(path, follow_symlinks=False)
    except FileNotFoundError as exc:
        raise GoogleCacheSecurityError(f"Google cache directory is missing: {path}") from exc
    if not stat.S_ISDIR(value.st_mode):
        raise GoogleCacheSecurityError(f"Google cache path is not a directory: {path}")
    if stat.S_IMODE(value.st_mode) & 0o077:
        raise GoogleCacheSecurityError(f"Google cache directory is not owner-only: {path}")
    return value


def _assert_private_file(path: Path) -> os.stat_result:
    try:
        value = os.stat(path, follow_symlinks=False)
    except FileNotFoundError as exc:
        raise GoogleCacheSecurityError(f"Google cache file is missing: {path}") from exc
    if not stat.S_ISREG(value.st_mode):
        raise GoogleCacheSecurityError(f"Google cache entry is not a regular file: {path}")
    if stat.S_IMODE(value.st_mode) & 0o077:
        raise GoogleCacheSecurityError(f"Google cache file is not owner-only: {path}")
    return value


def _atomic_private_json(path: Path, payload: dict[str, Any]) -> None:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    try:
        descriptor = os.open(temporary, flags, PRIVATE_FILE_MODE)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = None
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, PRIVATE_FILE_MODE)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary.exists() and not temporary.is_symlink():
            temporary.unlink()


def _read_json_file(path: Path) -> dict[str, Any]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "rb") as handle:
        try:
            payload = json.load(handle)
        except json.JSONDecodeError as exc:
            raise RuntimeError("Google cache entry contains invalid JSON") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("Google cache entry is not a JSON object")
    return payload


def _parse_cached_at(envelope: dict[str, Any]) -> datetime:
    value = str(envelope.get("cached_at") or "")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("Google cache entry has an invalid cached_at") from exc
    return _aware_utc(parsed)


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("Google cache timestamps must include a timezone")
    return value.astimezone(timezone.utc)


def _validated_connector_id(value: str) -> str:
    normalized = value.strip()
    if not _CONNECTOR_ID.fullmatch(normalized):
        raise ValueError("invalid Google cache connector id")
    return normalized
