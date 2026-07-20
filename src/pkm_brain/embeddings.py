from __future__ import annotations

import hashlib
import importlib.util
import math
import os
import re
from dataclasses import dataclass
from functools import cached_property
from typing import Any

import yaml


VECTOR_DIM = 384
HASH_PROVIDER = "hash"
HASH_MODEL = "hash-embedding-v1"
SENTENCE_TRANSFORMER_PROVIDER = "sentence-transformer"
DEFAULT_SENTENCE_TRANSFORMER_MODEL = "BAAI/bge-small-en-v1.5"
DEFAULT_BGE_QUERY_INSTRUCTION = "Represent this sentence for searching relevant passages: "
PASSAGE_EMBEDDING_MAX_CHARS = 6000


class EmbeddingProviderUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class EmbeddingConfig:
    provider: str = HASH_PROVIDER
    model: str = DEFAULT_SENTENCE_TRANSFORMER_MODEL
    query_instruction: str = ""


class EmbeddingProvider:
    provider = HASH_PROVIDER
    model_name = HASH_MODEL
    dim = VECTOR_DIM

    @property
    def name(self) -> str:
        return f"{self.provider}:{self.model_name}"

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [hash_embedding(text) for text in texts]

    def embed_queries(self, texts: list[str]) -> list[list[float]]:
        return self.embed(texts)

    def stamp(self) -> dict[str, Any]:
        return {"provider": self.provider, "model": self.model_name, "dim": self.dim}

    def status(self, check_available: bool = False) -> dict[str, Any]:
        return {
            "configured": self.provider,
            "model": self.model_name,
            "dim": self.dim,
            "available": True,
            "reason": None,
            "name": self.name,
        }


class SentenceTransformerProvider(EmbeddingProvider):
    provider = SENTENCE_TRANSFORMER_PROVIDER

    def __init__(
        self,
        model_name: str = DEFAULT_SENTENCE_TRANSFORMER_MODEL,
        query_instruction: str = "",
        *,
        cache_only: bool = True,
    ) -> None:
        self.model_name = model_name
        self.query_instruction = query_instruction or default_query_instruction_for_model(model_name)
        self.cache_only = cache_only

    @cached_property
    def model(self):
        os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
        os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:
            raise EmbeddingProviderUnavailable(
                "sentence-transformers extra is not installed; run `uv sync --extra embeddings`"
            ) from exc

        try:
            return SentenceTransformer(self.model_name, local_files_only=self.cache_only)
        except Exception as exc:
            cache_hint = " cached" if self.cache_only else ""
            raise EmbeddingProviderUnavailable(
                f"sentence-transformer{cache_hint} model unavailable ({self.model_name}): {exc}"
            ) from exc

    def embed(self, texts: list[str]) -> list[list[float]]:
        vectors = self.model.encode(texts, normalize_embeddings=True)
        return [list(map(float, row)) for row in vectors]

    def embed_queries(self, texts: list[str]) -> list[list[float]]:
        if not self.query_instruction:
            return self.embed(texts)
        return self.embed([f"{self.query_instruction}{text}" for text in texts])

    def status(self, check_available: bool = False) -> dict[str, Any]:
        available = True
        reason = None
        if check_available:
            try:
                _ = self.model
            except EmbeddingProviderUnavailable as exc:
                available = False
                reason = str(exc)
        elif importlib.util.find_spec("sentence_transformers") is None:
            available = False
            reason = "sentence-transformers extra is not installed; run `uv sync --extra embeddings`"
        return {
            "configured": self.provider,
            "model": self.model_name,
            "dim": self.dim,
            "available": available,
            "reason": reason,
            "name": self.name,
            "cache_only": self.cache_only,
            "query_instruction": self.query_instruction,
            "availability_checked": check_available,
        }


class UnavailableEmbeddingProvider(EmbeddingProvider):
    def __init__(self, provider: str, model_name: str, reason: str) -> None:
        self.provider = provider
        self.model_name = model_name
        self.reason = reason

    def embed(self, texts: list[str]) -> list[list[float]]:
        raise EmbeddingProviderUnavailable(self.reason)

    def status(self, check_available: bool = False) -> dict[str, Any]:
        return {
            "configured": self.provider,
            "model": self.model_name,
            "dim": self.dim,
            "available": False,
            "reason": self.reason,
            "name": self.name,
        }


def load_embedding_config(paths: Any | None = None) -> EmbeddingConfig:
    data: dict[str, Any] = {}
    if paths is not None and getattr(paths, "config_file", None) and paths.config_file.exists():
        loaded = yaml.safe_load(paths.config_file.read_text(encoding="utf-8")) or {}
        if isinstance(loaded, dict):
            data = loaded

    embedding = data.get("embedding") if isinstance(data.get("embedding"), dict) else {}
    provider = str(embedding.get("provider") or HASH_PROVIDER).strip().lower()
    model = str(
        embedding.get("model")
        or data.get("embedding_model")
        or DEFAULT_SENTENCE_TRANSFORMER_MODEL
    ).strip()
    query_instruction = str(embedding.get("query_instruction") or "")

    provider = os.environ.get("PKM_BRAIN_EMBEDDING_PROVIDER", provider).strip().lower()
    model = os.environ.get("PKM_BRAIN_EMBEDDING_MODEL", model).strip()
    query_instruction = os.environ.get("PKM_BRAIN_EMBEDDING_QUERY_INSTRUCTION", query_instruction)

    return EmbeddingConfig(
        provider=normalize_provider_name(provider),
        model=model or DEFAULT_SENTENCE_TRANSFORMER_MODEL,
        query_instruction=query_instruction,
    )


def save_embedding_config(
    paths: Any,
    *,
    provider: str,
    model: str | None = None,
    query_instruction: str | None = None,
) -> EmbeddingConfig:
    """Persist an embedding choice without discarding unrelated local config."""

    normalized_provider = normalize_provider_name(provider)
    if normalized_provider not in {HASH_PROVIDER, SENTENCE_TRANSFORMER_PROVIDER}:
        raise ValueError(
            "embedding provider must be 'hash' or 'sentence-transformer'"
        )
    config_path = paths.config_file
    data: dict[str, Any] = {}
    if config_path.exists():
        loaded = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        if isinstance(loaded, dict):
            data = loaded
    embedding = data.get("embedding")
    if not isinstance(embedding, dict):
        embedding = {}
        data["embedding"] = embedding
    embedding["provider"] = normalized_provider
    if model is not None:
        embedding["model"] = model.strip() or DEFAULT_SENTENCE_TRANSFORMER_MODEL
    elif not embedding.get("model"):
        embedding["model"] = DEFAULT_SENTENCE_TRANSFORMER_MODEL
    if query_instruction is not None:
        embedding["query_instruction"] = query_instruction

    config_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(config_path.parent, 0o700)
    temporary = config_path.with_name(f".{config_path.name}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    descriptor = os.open(temporary, flags, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            yaml.safe_dump(data, handle, sort_keys=True, allow_unicode=False)
        os.replace(temporary, config_path)
        os.chmod(config_path, 0o600)
    finally:
        if temporary.exists():
            temporary.unlink()
    return load_embedding_config(paths)


def resolve_embedding_provider(config: EmbeddingConfig, *, cache_only: bool = True) -> EmbeddingProvider:
    if config.provider == HASH_PROVIDER:
        return EmbeddingProvider()
    if config.provider == SENTENCE_TRANSFORMER_PROVIDER:
        return SentenceTransformerProvider(
            config.model,
            query_instruction=config.query_instruction,
            cache_only=cache_only,
        )
    return UnavailableEmbeddingProvider(
        config.provider,
        config.model,
        f"unsupported embedding provider {config.provider!r}; expected 'hash' or 'sentence-transformer'",
    )


def get_embedding_provider(paths: Any | None = None) -> EmbeddingProvider:
    return resolve_embedding_provider(load_embedding_config(paths))


def normalize_provider_name(provider: str) -> str:
    normalized = provider.strip().lower().replace("_", "-")
    if normalized in {"hash", "hash-embedding", "hash-embeddings"}:
        return HASH_PROVIDER
    if normalized in {"sentence-transformer", "sentence-transformers", "st", "model"}:
        return SENTENCE_TRANSFORMER_PROVIDER
    return normalized


def default_query_instruction_for_model(model_name: str) -> str:
    return DEFAULT_BGE_QUERY_INSTRUCTION if "bge" in model_name.lower() else ""


def passage_embedding_text(text: str, heading_path: str | None = None) -> str:
    body = text.strip()
    if len(body) > PASSAGE_EMBEDDING_MAX_CHARS:
        body = body[:PASSAGE_EMBEDDING_MAX_CHARS]
    heading = (heading_path or "").strip()
    if heading:
        return f"{heading}\n\n{body}"
    return body


def hash_embedding(text: str, dim: int = VECTOR_DIM) -> list[float]:
    vector = [0.0] * dim
    words = re.findall(r"[A-Za-z0-9_]+", text.lower())
    for word in words or [text]:
        digest = hashlib.blake2b(word.encode("utf-8"), digest_size=16).digest()
        index = int.from_bytes(digest[:4], "big") % dim
        sign = 1.0 if digest[4] % 2 == 0 else -1.0
        vector[index] += sign
    norm = math.sqrt(sum(value * value for value in vector)) or 1.0
    return [value / norm for value in vector]
