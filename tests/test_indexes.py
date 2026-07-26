from __future__ import annotations

from pathlib import Path
from typing import Any

from pkm_brain import indexes


class _FakeTable:
    def __init__(self) -> None:
        self.predicates: list[str] = []

    def delete(self, predicate: str) -> None:
        self.predicates.append(predicate)


class _FakeDatabase:
    def __init__(self, table: _FakeTable) -> None:
        self.table = table

    def open_table(self, _name: str) -> _FakeTable:
        return self.table


def test_delete_table_vectors_batches_large_predicates(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    db_path = tmp_path / "lancedb"
    db_path.mkdir()
    (db_path / "present").touch()
    table = _FakeTable()
    database = _FakeDatabase(table)
    monkeypatch.setattr(indexes.lancedb, "connect", lambda _path: database)
    monkeypatch.setattr(indexes, "table_names", lambda _database: ["chunks"])
    target_ids = [f"chunk_{index:04d}" for index in range(2049)]
    target_ids.append("chunk_with_'_quote")

    deleted = indexes.delete_table_vectors(
        db_path,
        "chunks",
        "chunk_id",
        target_ids,
    )

    assert deleted == 2050
    assert len(table.predicates) == 3
    assert all(predicate.startswith("chunk_id IN (") for predicate in table.predicates)
    assert [predicate.count("chunk_") - 1 for predicate in table.predicates] == [
        1024,
        1024,
        2,
    ]
    assert all(" OR " not in predicate for predicate in table.predicates)
    assert "chunk_with_''_quote" in table.predicates[-1]


def test_delete_table_vectors_handles_live_crash_threshold(tmp_path: Path) -> None:
    db_path = tmp_path / "lancedb"
    database = indexes.lancedb.connect(str(db_path))
    rows = [
        {
            "chunk_id": f"chunk_{index:04d}",
            "vector": [0.0, 0.0],
        }
        for index in range(700)
    ]
    database.create_table("chunks", rows)

    deleted = indexes.delete_table_vectors(
        db_path,
        "chunks",
        "chunk_id",
        [f"chunk_{index:04d}" for index in range(668)],
    )

    assert deleted == 668
    assert database.open_table("chunks").count_rows() == 32
