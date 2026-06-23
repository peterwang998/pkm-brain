from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from .db import connection, rows
from .paths import BrainPaths
from .util import now_iso
from .wiki import parse_frontmatter


CURATION_TABLES = ("facts", "open_questions", "wiki_curation_runs")
PROMOTION_NAME = "wiki_curation_promote_v1"


def promote_wiki_curation(
    source_paths: BrainPaths,
    target_paths: BrainPaths,
    *,
    dry_run: bool = True,
    replace_existing: bool = False,
    backup: bool = True,
) -> dict[str, Any]:
    """Promote resolved chief-of-staff curation state from a fork into a target Brain.

    This intentionally avoids replacing the target Brain DB wholesale. It imports
    the source fact-ledger state, copies managed wiki files, and updates proposal
    statuses for shared proposal/change IDs while leaving target-only documents,
    chunks, memories, and proposal rows intact.
    """
    source_paths = BrainPaths.from_value(source_paths.home)
    target_paths = BrainPaths.from_value(target_paths.home)
    if source_paths.home == target_paths.home:
        raise ValueError("source and target Brain homes must be different")
    if not source_paths.sqlite_path.exists():
        raise ValueError(f"source sqlite not found: {source_paths.sqlite_path}")
    if not target_paths.sqlite_path.exists():
        raise ValueError(f"target sqlite not found: {target_paths.sqlite_path}")

    plan = promotion_plan(source_paths, target_paths)
    if dry_run:
        return {**plan, "dry_run": True, "applied": False, "backup_dir": None}

    target_counts = target_curation_counts(target_paths)
    existing_rows = sum(target_counts.get(table, 0) for table in CURATION_TABLES)
    if existing_rows and not replace_existing:
        raise ValueError(
            "target already has curation rows; rerun with --replace-existing after reviewing the dry run"
        )

    backup_dir = create_promotion_backup(target_paths) if backup else None
    copy_managed_wiki_files(source_paths, target_paths)
    with connection(target_paths.sqlite_path) as conn:
        conn.execute("ATTACH DATABASE ? AS source", (str(source_paths.sqlite_path),))
        for table in CURATION_TABLES:
            conn.execute(f"DELETE FROM {table}")
            conn.execute(f"INSERT INTO {table} SELECT * FROM source.{table}")
        conn.execute(
            """
            UPDATE wiki_change_batches
            SET status = (
              SELECT source.wiki_change_batches.status
              FROM source.wiki_change_batches
              WHERE source.wiki_change_batches.id = wiki_change_batches.id
            )
            WHERE id IN (SELECT id FROM source.wiki_change_batches)
            """
        )
        conn.execute(
            """
            UPDATE wiki_change_items
            SET status = (
              SELECT source.wiki_change_items.status
              FROM source.wiki_change_items
              WHERE source.wiki_change_items.id = wiki_change_items.id
            )
            WHERE id IN (SELECT id FROM source.wiki_change_items)
            """
        )
        upsert_source_managed_wiki_pages(conn)

    applied = promotion_plan(source_paths, target_paths)
    return {
        **applied,
        "dry_run": False,
        "applied": True,
        "backup_dir": str(backup_dir) if backup_dir else None,
    }


def promotion_plan(source_paths: BrainPaths, target_paths: BrainPaths) -> dict[str, Any]:
    source_counts = source_curation_counts(source_paths)
    target_counts = target_curation_counts(target_paths)
    managed_files = managed_wiki_files(source_paths)
    with connection(target_paths.sqlite_path) as conn:
        conn.execute("ATTACH DATABASE ? AS source", (str(source_paths.sqlite_path),))
        shared_batches = conn.execute(
            """
            SELECT COUNT(*)
            FROM wiki_change_batches
            WHERE id IN (SELECT id FROM source.wiki_change_batches)
            """
        ).fetchone()[0]
        shared_items = conn.execute(
            """
            SELECT COUNT(*)
            FROM wiki_change_items
            WHERE id IN (SELECT id FROM source.wiki_change_items)
            """
        ).fetchone()[0]
        target_only_batches = conn.execute(
            """
            SELECT COUNT(*)
            FROM wiki_change_batches
            WHERE id NOT IN (SELECT id FROM source.wiki_change_batches)
            """
        ).fetchone()[0]
        target_only_items = conn.execute(
            """
            SELECT COUNT(*)
            FROM wiki_change_items
            WHERE id NOT IN (SELECT id FROM source.wiki_change_items)
            """
        ).fetchone()[0]
    existing_target_managed = sum(1 for path in managed_files if (target_paths.wiki / path).exists())
    return {
        "promotion": PROMOTION_NAME,
        "source_home": str(source_paths.home),
        "target_home": str(target_paths.home),
        "source_counts": source_counts,
        "target_counts": target_counts,
        "managed_wiki_files": len(managed_files),
        "managed_wiki_files_overwritten": existing_target_managed,
        "managed_wiki_file_preview": [path.as_posix() for path in managed_files[:20]],
        "shared_batches_to_update": int(shared_batches),
        "shared_items_to_update": int(shared_items),
        "target_only_batches_preserved": int(target_only_batches),
        "target_only_items_preserved": int(target_only_items),
    }


def source_curation_counts(paths: BrainPaths) -> dict[str, int]:
    with connection(paths.sqlite_path) as conn:
        return {
            **table_counts(conn, CURATION_TABLES),
            "managed_wiki_pages": int(
                conn.execute("SELECT COUNT(*) FROM wiki_pages WHERE managed = 1").fetchone()[0]
            ),
        }


def target_curation_counts(paths: BrainPaths) -> dict[str, int]:
    return source_curation_counts(paths)


def table_counts(conn: Any, table_names: tuple[str, ...]) -> dict[str, int]:
    return {
        table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in table_names
    }


def managed_wiki_files(paths: BrainPaths) -> list[Path]:
    if not paths.wiki.exists():
        return []
    output: list[Path] = []
    for path in sorted(paths.wiki.rglob("*.md")):
        text = path.read_text(encoding="utf-8", errors="replace")
        frontmatter, _ = parse_frontmatter(text)
        if frontmatter is None:
            continue
        tags = frontmatter.get("tags") or []
        managed = (
            bool(frontmatter.get("managed"))
            or "managed" in tags
            or str(frontmatter.get("id") or "").startswith("managed-")
            or "generated-by: pkm-brain chief-of-staff-facts" in text
        )
        if managed:
            output.append(path.relative_to(paths.wiki))
    return output


def copy_managed_wiki_files(source_paths: BrainPaths, target_paths: BrainPaths) -> None:
    for relative_path in managed_wiki_files(source_paths):
        source = source_paths.wiki / relative_path
        target = target_paths.wiki / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def create_promotion_backup(paths: BrainPaths) -> Path:
    stamp = now_iso().replace(":", "").replace("+", "_").replace("-", "")
    backup_dir = paths.logs / "curation-promotion-backups" / stamp
    backup_dir.mkdir(parents=True, exist_ok=False)
    if paths.sqlite_path.exists():
        (backup_dir / "db").mkdir(parents=True, exist_ok=True)
        shutil.copy2(paths.sqlite_path, backup_dir / "db" / paths.sqlite_path.name)
    if paths.wiki.exists():
        shutil.copytree(paths.wiki, backup_dir / "wiki")
    return backup_dir


def upsert_source_managed_wiki_pages(conn: Any) -> None:
    source_rows = rows(
        conn,
        """
        SELECT *
        FROM source.wiki_pages
        WHERE managed = 1
        """,
    )
    for row in source_rows:
        conn.execute(
            """
            INSERT INTO wiki_pages(
              id, title, page_type, status, path, source_ids, related, tags,
              created_at, updated_at, managed, fact_ids
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
              title = excluded.title,
              page_type = excluded.page_type,
              status = excluded.status,
              path = excluded.path,
              source_ids = excluded.source_ids,
              related = excluded.related,
              tags = excluded.tags,
              created_at = excluded.created_at,
              updated_at = excluded.updated_at,
              managed = excluded.managed,
              fact_ids = excluded.fact_ids
            """,
            (
                row["id"],
                row["title"],
                row["page_type"],
                row["status"],
                row["path"],
                row["source_ids"],
                row["related"],
                row["tags"],
                row["created_at"],
                row["updated_at"],
                row["managed"],
                row["fact_ids"],
            ),
        )
