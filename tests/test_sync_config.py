from __future__ import annotations

from pathlib import Path

import pytest

from pkm_brain.paths import BrainPaths
from pkm_brain.sync_config import load_sync_config


def write_sync(paths: BrainPaths, text: str) -> None:
    paths.config.mkdir(parents=True, exist_ok=True)
    paths.sync_config_file.write_text(text, encoding="utf-8")


def test_valid_primary_config_parses(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    write_sync(
        paths,
        """
node_id: primary-laptop
role: primary
brain_home: ~/brain
peers:
  - node_id: secondary-desktop
    role: secondary
    host: secondary.local
    user: peter
    brain_home: ~/brain
    mirror_paths:
      - outbox/secondary-desktop
""",
    )

    config = load_sync_config(paths)

    assert config.role == "primary"
    assert config.node_id == "primary-laptop"
    assert config.primary is not None
    assert config.primary.peers[0].node_id == "secondary-desktop"


def test_valid_secondary_config_parses(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    write_sync(
        paths,
        """
node_id: secondary-desktop
role: secondary
brain_home: ~/brain
primary:
  node_id: primary-laptop
  expected_user: peter
outbox:
  enabled: true
  path: ~/brain/outbox/secondary-desktop
""",
    )

    config = load_sync_config(paths)

    assert config.role == "secondary"
    assert config.secondary is not None
    assert config.secondary.primary_node_id == "primary-laptop"


def test_missing_role_raises(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    write_sync(paths, "node_id: primary-laptop\n")

    with pytest.raises(ValueError, match="role"):
        load_sync_config(paths)


def test_duplicate_peer_node_id_raises(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    write_sync(
        paths,
        """
node_id: primary-laptop
role: primary
peers:
  - node_id: secondary
  - node_id: secondary
""",
    )

    with pytest.raises(ValueError, match="duplicate"):
        load_sync_config(paths)


def test_forbidden_mirror_path_raises(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    write_sync(
        paths,
        """
node_id: primary-laptop
role: primary
peers:
  - node_id: secondary
    mirror_paths:
      - db/brain.sqlite
""",
    )

    with pytest.raises(ValueError, match="local-only"):
        load_sync_config(paths)


def test_secondary_outbox_must_include_node_id(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    write_sync(
        paths,
        """
node_id: secondary-desktop
role: secondary
primary:
  node_id: primary-laptop
outbox:
  path: ~/brain/outbox/wrong-node
""",
    )

    with pytest.raises(ValueError, match="node_id"):
        load_sync_config(paths)
