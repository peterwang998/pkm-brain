from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class IngestResult:
    run_id: str
    discovered: int
    changed: int
    skipped: int
    chunks_created: int
    embeddings_created: int
    errors: list[str]
    documents_replaced: int = 0
    vector_writes: dict[str, Any] = field(default_factory=dict)
    deferred: int = 0

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class IngestBudget:
    """Bound changed-source work while always allowing one source to advance."""

    max_documents: int | None = None
    max_source_bytes: int | None = None
    attempted_documents: int = 0
    attempted_source_bytes: int = 0

    def __post_init__(self) -> None:
        if self.max_documents is not None and self.max_documents <= 0:
            raise ValueError("max_changed_documents must be positive")
        if self.max_source_bytes is not None and self.max_source_bytes <= 0:
            raise ValueError("max_changed_source_bytes must be positive")

    def accept(self, source_size: int) -> bool:
        if self.max_documents is not None and self.attempted_documents >= self.max_documents:
            return False
        if (
            self.max_source_bytes is not None
            and self.attempted_documents > 0
            and self.attempted_source_bytes + source_size > self.max_source_bytes
        ):
            return False
        self.attempted_documents += 1
        self.attempted_source_bytes += source_size
        return True
