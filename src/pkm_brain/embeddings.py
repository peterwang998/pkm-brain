from __future__ import annotations

import hashlib
import math
import re
from functools import cached_property


VECTOR_DIM = 384


class EmbeddingProvider:
    name = "hash-embedding"

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [hash_embedding(text) for text in texts]


class SentenceTransformerProvider(EmbeddingProvider):
    def __init__(self, model_name: str = "BAAI/bge-small-en-v1.5") -> None:
        self.model_name = model_name
        self.name = model_name

    @cached_property
    def model(self):
        from sentence_transformers import SentenceTransformer

        return SentenceTransformer(self.model_name)

    def embed(self, texts: list[str]) -> list[list[float]]:
        vectors = self.model.encode(texts, normalize_embeddings=True)
        return [list(map(float, row)) for row in vectors]


def get_embedding_provider(prefer_model: bool = True) -> EmbeddingProvider:
    if prefer_model:
        try:
            provider = SentenceTransformerProvider()
            provider.embed(["health check"])
            return provider
        except Exception:
            return EmbeddingProvider()
    return EmbeddingProvider()


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
