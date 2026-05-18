from __future__ import annotations

from pathlib import Path
from typing import Any

import lancedb

from .embeddings import EmbeddingProvider


TABLE_NAME = "chunks"


def upsert_vectors(db_path: Path, rows: list[dict[str, Any]]) -> int:
    if not rows:
        return 0
    db_path.mkdir(parents=True, exist_ok=True)
    db = lancedb.connect(str(db_path))
    if TABLE_NAME in table_names(db):
        table = db.open_table(TABLE_NAME)
        try:
            table.delete(" OR ".join(f"chunk_id = '{row['chunk_id']}'" for row in rows))
        except Exception:
            pass
        table.add(rows)
    else:
        try:
            db.create_table(TABLE_NAME, rows)
        except ValueError as exc:
            if "already exists" not in str(exc):
                raise
            table = db.open_table(TABLE_NAME)
            table.add(rows)
    return len(rows)


def delete_vectors(db_path: Path, chunk_ids: list[str]) -> int:
    if not chunk_ids or not (db_path.exists() and any(db_path.iterdir())):
        return 0
    db = lancedb.connect(str(db_path))
    if TABLE_NAME not in table_names(db):
        return 0
    table = db.open_table(TABLE_NAME)
    table.delete(" OR ".join(f"chunk_id = '{chunk_id}'" for chunk_id in chunk_ids))
    try:
        table.optimize()
    except Exception:
        pass
    return len(chunk_ids)


def search_vectors(db_path: Path, provider: EmbeddingProvider, query: str, limit: int) -> list[dict[str, Any]]:
    if not (db_path.exists() and any(db_path.iterdir())):
        return []
    db = lancedb.connect(str(db_path))
    if TABLE_NAME not in table_names(db):
        return []
    table = db.open_table(TABLE_NAME)
    vector = provider.embed([query])[0]
    try:
        results = table.search(vector).limit(limit).to_list()
    except Exception:
        return []
    return [dict(row) for row in results]


def table_names(db: Any) -> list[str]:
    if hasattr(db, "list_tables"):
        result = db.list_tables()
        if hasattr(result, "tables"):
            return list(result.tables)
        return list(result)
    return list(db.table_names())
