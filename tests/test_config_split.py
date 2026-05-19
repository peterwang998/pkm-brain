from __future__ import annotations

import warnings
from pathlib import Path

import pkm_brain.paths as paths_module
from pkm_brain.paths import BrainPaths
from pkm_brain.service import BrainService


def test_init_workspace_creates_local_and_shared_config_dirs(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths, prefer_model_embeddings=False).init_workspace()

    assert paths.config_local.is_dir()
    assert paths.config_shared.is_dir()
    assert paths.config_file.exists()
    assert not paths.legacy_config_file.exists()


def test_legacy_config_read_shim_warns_once(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    paths.config.mkdir(parents=True)
    paths.legacy_config_file.write_text("brain_home: legacy\n", encoding="utf-8")
    paths_module._warned_legacy_config = False

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        first = paths.config_file_for_read()
        second = paths.config_file_for_read()

    assert first == paths.legacy_config_file
    assert second == paths.legacy_config_file
    assert len([warning for warning in caught if issubclass(warning.category, DeprecationWarning)]) == 1
