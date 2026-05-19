from __future__ import annotations

from pathlib import Path

from pkm_brain.paths import BrainPaths
from pkm_brain.sync_config import PeerConfig
from pkm_brain.sync_rsync import PUSH_SOURCE_SUBDIRS, build_pull, build_push


def peer(remote_home: Path) -> PeerConfig:
    return PeerConfig(node_id="secondary", host="secondary.local", user="peter", brain_home=remote_home)


def test_pull_builder_targets_staging_not_live_external_inbox(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "primary")
    argv = build_pull(paths, peer(tmp_path / "secondary"), "run-1")

    assert argv[:4] == ["rsync", "-az", "--delete", "-e"]
    assert argv[-2] == f"peter@secondary.local:{tmp_path}/secondary/outbox/secondary/"
    assert argv[-1] == f"{paths.inbox}/external/secondary/_staging/run-1/"
    assert f"{paths.inbox}/external/secondary/" != argv[-1]


def test_pull_builder_uses_peer_outbox_path_when_configured(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "primary")
    secondary = PeerConfig(
        node_id="secondary",
        host="secondary.local",
        user="peter",
        brain_home=tmp_path / "secondary",
        outbox_path=tmp_path / "custom-outbox" / "secondary",
    )

    argv = build_pull(paths, secondary, "run-1")

    assert argv[-2] == f"peter@secondary.local:{tmp_path}/custom-outbox/secondary/"


def test_push_builder_includes_atomic_flags_and_excludes_for_each_source(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "primary")
    secondary = peer(tmp_path / "secondary")

    for source_subdir in PUSH_SOURCE_SUBDIRS:
        argv = build_push(paths, secondary, source_subdir)

        assert "--delay-updates" in argv
        assert "--partial-dir=.rsync-partial" in argv
        for pattern in [
            "db/",
            "indexes/",
            "logs/",
            "*.sqlite",
            "*.sqlite-wal",
            "*.sqlite-shm",
            ".DS_Store",
            "cache/",
            "tmp/",
            "config/sync.yaml",
            "config/local/",
            "outbox/",
        ]:
            assert pattern in argv
        assert argv[-2] == f"{paths.home}/{source_subdir}"
        assert argv[-1] == f"peter@secondary.local:{tmp_path}/secondary/{source_subdir}"


def test_push_builder_rejects_unsupported_source_subdir(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "primary")

    try:
        build_push(paths, peer(tmp_path / "secondary"), "db/")
    except ValueError as exc:
        assert "unsupported sync push source subdir" in str(exc)
    else:
        raise AssertionError("expected invalid source subdir to fail")
