from __future__ import annotations

from pathlib import Path

from pkm_brain.embeddings import SentenceTransformerProvider, load_embedding_config
from pkm_brain.paths import BrainPaths
from pkm_brain.service import BrainService


def test_init_workspace_creates_local_and_shared_config_dirs(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()

    assert paths.config_local.is_dir()
    assert paths.config_shared.is_dir()
    assert paths.config_file.exists()
    assert "embedding:" in paths.config_file.read_text(encoding="utf-8")


def test_embedding_config_reads_nested_config_env_and_legacy_key(tmp_path: Path, monkeypatch) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    paths.config_local.mkdir(parents=True)
    paths.config_file.write_text(
        "embedding_model: legacy-model\n"
        "embedding:\n"
        "  provider: sentence-transformer\n"
        "  model: configured-model\n"
        "  query_instruction: 'query: '\n",
        encoding="utf-8",
    )

    config = load_embedding_config(paths)

    assert config.provider == "sentence-transformer"
    assert config.model == "configured-model"
    assert config.query_instruction == "query: "

    monkeypatch.setenv("PKM_BRAIN_EMBEDDING_PROVIDER", "hash")
    monkeypatch.setenv("PKM_BRAIN_EMBEDDING_MODEL", "env-model")
    overridden = load_embedding_config(paths)
    assert overridden.provider == "hash"
    assert overridden.model == "env-model"


def test_embedding_config_reads_legacy_model_key(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    paths.config_local.mkdir(parents=True)
    paths.config_file.write_text("embedding_model: legacy-model\n", encoding="utf-8")

    config = load_embedding_config(paths)

    assert config.provider == "hash"
    assert config.model == "legacy-model"


def test_sentence_transformer_query_instruction_is_asymmetric() -> None:
    captured: list[str] = []

    class CapturingProvider(SentenceTransformerProvider):
        def embed(self, texts: list[str]) -> list[list[float]]:
            captured.extend(texts)
            return [[0.0] * 384 for _ in texts]

    provider = CapturingProvider("BAAI/bge-small-en-v1.5", query_instruction="query: ")

    provider.embed_queries(["semantic retrieval"])
    provider.embed(["semantic retrieval"])

    assert captured == ["query: semantic retrieval", "semantic retrieval"]
