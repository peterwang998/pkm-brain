from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

import lancedb

from .embeddings import EmbeddingProvider


TABLE_NAME = "chunks"


def upsert_vectors(db_path: Path, rows: list[dict[str, Any]]) -> int:
    return upsert_table_vectors(db_path, TABLE_NAME, "chunk_id", rows)


def upsert_table_vectors(
    db_path: Path, table_name: str, id_column: str, rows: list[dict[str, Any]]
) -> int:
    if not rows:
        return 0
    db_path.mkdir(parents=True, exist_ok=True)
    db = lancedb.connect(str(db_path))
    if table_name in table_names(db):
        table = db.open_table(table_name)
        try:
            table.delete(" OR ".join(f"{id_column} = '{row[id_column]}'" for row in rows))
        except Exception:
            pass
        table.add(rows)
    else:
        try:
            db.create_table(table_name, rows)
        except ValueError as exc:
            if "already exists" not in str(exc):
                raise
            table = db.open_table(table_name)
            table.add(rows)
    return len(rows)


def delete_vectors(db_path: Path, chunk_ids: list[str]) -> int:
    return delete_table_vectors(db_path, TABLE_NAME, "chunk_id", chunk_ids)


def delete_table_vectors(
    db_path: Path, table_name: str, id_column: str, target_ids: list[str]
) -> int:
    if not target_ids or not (db_path.exists() and any(db_path.iterdir())):
        return 0
    db = lancedb.connect(str(db_path))
    if table_name not in table_names(db):
        return 0
    table = db.open_table(table_name)
    table.delete(" OR ".join(f"{id_column} = '{target_id}'" for target_id in target_ids))
    return len(target_ids)


def search_vectors(db_path: Path, provider: EmbeddingProvider, query: str, limit: int) -> list[dict[str, Any]]:
    return search_table_vectors(db_path, provider, query, limit, table_name=TABLE_NAME)


def search_table_vectors(
    db_path: Path,
    provider: EmbeddingProvider,
    query: str,
    limit: int,
    *,
    table_name: str,
) -> list[dict[str, Any]]:
    if not (db_path.exists() and any(db_path.iterdir())):
        return []
    db = lancedb.connect(str(db_path))
    if table_name not in table_names(db):
        return []
    table = db.open_table(table_name)
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


def lancedb_stats(db_path: Path) -> dict[str, Any]:
    total_bytes = path_size(db_path)
    table_path = db_path / f"{TABLE_NAME}.lance"
    data_path = table_path / "data"
    stats: dict[str, Any] = {
        "path": str(db_path),
        "exists": db_path.exists(),
        "table_exists": False,
        "rows": 0,
        "versions": 0,
        "data_files": count_files(data_path),
        "data_bytes": path_size(data_path),
        "total_bytes": total_bytes,
        "latest_table_bytes": 0,
        "retained_version_bytes": 0,
        "latest_version": None,
        "latest_metadata": {},
    }
    if not (db_path.exists() and any(db_path.iterdir())):
        return stats
    db = lancedb.connect(str(db_path))
    if TABLE_NAME not in table_names(db):
        return stats
    table = db.open_table(TABLE_NAME)
    versions = table.list_versions()
    latest = versions[-1] if versions else None
    latest_metadata = dict(latest.get("metadata") or {}) if latest else {}
    latest_table_bytes = int(latest_metadata.get("total_files_size") or 0)
    stats.update(
        {
            "table_exists": True,
            "rows": table.count_rows(),
            "versions": len(versions),
            "latest_version": latest.get("version") if latest else None,
            "latest_metadata": latest_metadata,
            "latest_table_bytes": latest_table_bytes,
            "retained_version_bytes": max(0, total_bytes - latest_table_bytes),
        }
    )
    return stats


def optimize_vectors(db_path: Path, cleanup_older_than_days: int = 1) -> dict[str, Any]:
    before = lancedb_stats(db_path)
    if not before["table_exists"]:
        return {
            "status": "skipped",
            "reason": "lancedb table does not exist",
            "before": before,
            "after": before,
            "bytes_freed": 0,
            "optimize": {},
        }
    db = lancedb.connect(str(db_path))
    table = db.open_table(TABLE_NAME)
    result = table.optimize(cleanup_older_than=timedelta(days=cleanup_older_than_days), delete_unverified=False)
    after = lancedb_stats(db_path)
    return {
        "status": "ok",
        "before": before,
        "after": after,
        "bytes_freed": max(0, int(before["total_bytes"]) - int(after["total_bytes"])),
        "optimize": serializable_stats(result),
    }


def vector_chunk_ids(db_path: Path) -> set[str]:
    if not (db_path.exists() and any(db_path.iterdir())):
        return set()
    db = lancedb.connect(str(db_path))
    if TABLE_NAME not in table_names(db):
        return set()
    table = db.open_table(TABLE_NAME)
    arrow_table = table.to_arrow()
    if "chunk_id" not in arrow_table.column_names:
        return set()
    return {str(value) for value in arrow_table.column("chunk_id").to_pylist() if value}


def should_optimize_vectors(
    stats: dict[str, Any],
    version_threshold: int = 250,
    data_file_threshold: int = 100,
    retained_bytes_threshold: int = 512 * 1024 * 1024,
) -> bool:
    return (
        bool(stats.get("table_exists"))
        and (
            int(stats.get("versions") or 0) > version_threshold
            or int(stats.get("data_files") or 0) > data_file_threshold
            or int(stats.get("retained_version_bytes") or 0) > retained_bytes_threshold
        )
    )


def path_size(path: Path) -> int:
    if not path.exists():
        return 0
    if path.is_file():
        return path.stat().st_size
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def count_files(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(1 for item in path.rglob("*") if item.is_file())


def serializable_stats(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if hasattr(value, "__dict__"):
        return {key: serializable_value(item) for key, item in vars(value).items()}
    if isinstance(value, dict):
        return {str(key): serializable_value(item) for key, item in value.items()}
    return {"repr": repr(value)}


def serializable_value(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, dict):
        return {str(key): serializable_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [serializable_value(item) for item in value]
    return repr(value)
