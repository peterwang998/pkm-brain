from __future__ import annotations

from pathlib import Path

from pkm_brain.paths import BrainPaths
from pkm_brain.service import BrainService


def test_sync_doctor_passes_for_primary_workspace(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()
    paths.sync_config_file.write_text(
        f"""
node_id: primary-laptop
role: primary
brain_home: {paths.home}
peers: []
""",
        encoding="utf-8",
    )

    result = BrainService(paths).sync_doctor()

    assert result["role"] == "primary"
    assert result["node_id"] == "primary-laptop"
    assert result["ready"] is True
    assert isinstance(result["checks"], list)


def test_sync_doctor_passes_for_secondary_workspace(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()
    paths.sync_config_file.write_text(
        f"""
node_id: secondary-desktop
role: secondary
brain_home: {paths.home}
primary:
  node_id: primary-laptop
outbox:
  enabled: true
  path: {paths.outbox / "secondary-desktop"}
""",
        encoding="utf-8",
    )

    result = BrainService(paths).sync_doctor()

    assert result["role"] == "secondary"
    assert result["node_id"] == "secondary-desktop"
    assert result["ready"] is True


def test_sync_doctor_flags_missing_config(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()

    result = BrainService(paths).sync_doctor()

    assert result["ready"] is False
    assert any(check["name"] == "sync_config_exists" and check["status"] == "fail" for check in result["checks"])
