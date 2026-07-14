from __future__ import annotations

import os
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from pkm_brain.google_cache import GoogleCacheSecurityError, GoogleEvidenceCache


NOW = datetime(2026, 7, 13, 12, tzinfo=timezone.utc)


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_cache_round_trips_private_raw_and_normalized_evidence(tmp_path: Path) -> None:
    cache = GoogleEvidenceCache(tmp_path / "google-cache")

    raw_path = cache.write_raw(
        "gmail",
        "thread/one",
        {"id": "thread-1", "messages": []},
        cached_at=NOW,
    )
    normalized_path = cache.write_normalized(
        "calendar",
        "primary:event-1",
        {"event_id": "event-1", "title": "Review"},
        cached_at=NOW,
    )

    assert cache.read_raw("gmail", "thread/one") == {
        "id": "thread-1",
        "messages": [],
    }
    assert cache.read_normalized("calendar", "primary:event-1") == {
        "event_id": "event-1",
        "title": "Review",
    }
    assert mode(cache.root) == 0o700
    assert mode(raw_path.parent) == 0o700
    assert mode(normalized_path.parent) == 0o700
    assert mode(raw_path) == 0o600
    assert mode(normalized_path) == 0o600
    assert "thread" not in raw_path.name


def test_cache_retention_is_seven_days_raw_and_thirty_days_normalized(
    tmp_path: Path,
) -> None:
    cache = GoogleEvidenceCache(tmp_path / "google-cache")
    cache.write_raw(
        "gmail",
        "old-raw",
        {"id": "old"},
        cached_at=NOW - timedelta(days=7, seconds=1),
    )
    cache.write_raw(
        "gmail",
        "fresh-raw",
        {"id": "fresh"},
        cached_at=NOW - timedelta(days=7),
    )
    cache.write_normalized(
        "gmail",
        "middle-normalized",
        {"id": "middle"},
        cached_at=NOW - timedelta(days=20),
    )
    cache.write_normalized(
        "calendar",
        "old-normalized",
        {"id": "old"},
        cached_at=NOW - timedelta(days=30, seconds=1),
    )

    result = cache.prune(now=NOW)

    assert result.removed_files == 2
    assert result.retained_files == 2
    assert result.removed_bytes > 0
    assert result.errors == ()
    assert cache.read_raw("gmail", "old-raw") is None
    assert cache.read_raw("gmail", "fresh-raw") == {"id": "fresh"}
    assert cache.read_normalized("gmail", "middle-normalized") == {"id": "middle"}
    assert cache.read_normalized("calendar", "old-normalized") is None


def test_cache_rejects_symlinks_and_non_owner_permissions(tmp_path: Path) -> None:
    root = tmp_path / "google-cache"
    cache = GoogleEvidenceCache(root)
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    connector_dir = cache.raw_root / "gmail"
    connector_dir.mkdir(mode=0o700)
    (connector_dir / "link.json").symlink_to(outside)

    result = cache.prune(now=NOW)

    assert result.removed_files == 0
    assert result.errors and "not a regular file" in result.errors[0]
    assert outside.exists()

    path = cache.write_raw("gmail", "private", {"ok": True}, cached_at=NOW)
    os.chmod(path, 0o644)
    with pytest.raises(GoogleCacheSecurityError, match="not owner-only"):
        cache.read_raw("gmail", "private")


def test_cache_rejects_symlink_root_and_unsafe_connector_id(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(real, target_is_directory=True)

    with pytest.raises(GoogleCacheSecurityError, match="must not be a symlink"):
        GoogleEvidenceCache(linked)

    cache = GoogleEvidenceCache(tmp_path / "safe")
    with pytest.raises(ValueError, match="connector id"):
        cache.write_raw("../gmail", "thread", {})


def test_cache_requires_timezone_aware_retention_timestamps(tmp_path: Path) -> None:
    cache = GoogleEvidenceCache(tmp_path / "google-cache")

    with pytest.raises(ValueError, match="timezone"):
        cache.write_raw("gmail", "thread", {}, cached_at=datetime(2026, 7, 13))
