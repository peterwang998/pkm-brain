from __future__ import annotations

from pathlib import Path

import yaml

from pkm_brain.embeddings import (
    DEFAULT_SENTENCE_TRANSFORMER_MODEL,
    load_embedding_config,
    save_embedding_config,
)
from pkm_brain.paths import BrainPaths


def test_save_embedding_config_preserves_other_local_settings(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.delenv("PKM_BRAIN_EMBEDDING_PROVIDER", raising=False)
    monkeypatch.delenv("PKM_BRAIN_EMBEDDING_MODEL", raising=False)
    paths = BrainPaths.from_value(tmp_path / "brain")
    paths.config_local.mkdir(parents=True)
    paths.config_file.write_text(
        "feature_flags:\n  temporal: true\nembedding:\n  provider: hash\n",
        encoding="utf-8",
    )

    configured = save_embedding_config(
        paths,
        provider="sentence-transformers",
        model=DEFAULT_SENTENCE_TRANSFORMER_MODEL,
    )

    assert configured.provider == "sentence-transformer"
    assert configured.model == DEFAULT_SENTENCE_TRANSFORMER_MODEL
    persisted = yaml.safe_load(paths.config_file.read_text(encoding="utf-8"))
    assert persisted["feature_flags"] == {"temporal": True}
    assert persisted["embedding"]["provider"] == "sentence-transformer"
    assert paths.config_file.stat().st_mode & 0o777 == 0o600
    assert paths.config_local.stat().st_mode & 0o777 == 0o700
    assert load_embedding_config(paths) == configured


def test_save_embedding_config_rejects_unknown_provider(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")

    try:
        save_embedding_config(paths, provider="mystery")
    except ValueError as exc:
        assert "embedding provider" in str(exc)
    else:
        raise AssertionError("unknown embedding provider should fail closed")
