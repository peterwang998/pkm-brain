from __future__ import annotations

from pathlib import Path

from pkm_brain.paths import BrainPaths
from pkm_brain.service import BrainService


def test_init_workspace_creates_local_and_shared_config_dirs(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths, prefer_model_embeddings=False).init_workspace()

    assert paths.config_local.is_dir()
    assert paths.config_shared.is_dir()
    assert paths.config_file.exists()
