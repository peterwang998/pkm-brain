from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
from contextlib import closing, contextmanager
from importlib import metadata
from pathlib import Path
from typing import Any, Iterator

from .migrations import MIGRATIONS
from .operational_migrations import OPERATIONAL_MIGRATIONS
from .operational_service import OperationalService
from .paths import BrainPaths
from .sync_config import load_sync_config
from .sync_status import canonical_manifest_hash
from .util import file_sha256, new_id, now_iso


RECOVERY_FORMAT_VERSION = 1
RECOVERY_DIRECTORY_MODE = 0o700
RECOVERY_FILE_MODE = 0o600
RECOVERY_BUSY_TIMEOUT_MS = 5000
KNOWLEDGE_DATABASE_FILENAME = "brain.sqlite"
OPERATIONAL_DATABASE_FILENAME = "ops.sqlite"
MANIFEST_FILENAME = "manifest.json"
COMMITTED_FILENAME = "COMMITTED"
BRAIN_ID_PATTERN = re.compile(r"^brain_[0-9a-f]{16}$")
BACKUP_SET_ID_PATTERN = re.compile(r"^recovery_[0-9a-f]{16}$")
REQUIRED_KNOWLEDGE_TABLES = {
    "schema_migrations",
    "documents",
    "facts",
    "cos_actions",
}
REQUIRED_OPERATIONAL_TABLES = {
    "ops_schema_migrations",
    "ops_observations",
    "ops_items",
    "ops_item_events",
    "ops_source_cursors",
}
SCHEMA_METADATA_VERSION = 1


def recovery_scope() -> dict[str, Any]:
    """Describe the intentionally narrow contents of a recovery set."""

    return {
        "included": [
            "db/brain.sqlite",
            "db/ops.sqlite",
        ],
        "excluded": [
            "Gmail encrypted archive and mailbox mirror",
            "raw source files and inbox captures",
            "wiki files, indexes, configuration, logs, and runtime files",
        ],
        "complete_brain_home": False,
    }


class RecoverySetError(RuntimeError):
    """A coordinated database-pair recovery artifact is unsafe or invalid."""


def _package_version() -> str:
    try:
        return metadata.version("pkm-brain")
    except metadata.PackageNotFoundError:
        return "0.0.0+local"


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_private_file(path: Path, content: bytes) -> None:
    path.parent.mkdir(mode=RECOVERY_DIRECTORY_MODE, parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    if temporary.exists() or temporary.is_symlink():
        raise RecoverySetError(f"temporary recovery path already exists: {temporary}")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(temporary, flags, RECOVERY_FILE_MODE)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(descriptor)
    os.replace(temporary, path)
    os.chmod(path, RECOVERY_FILE_MODE)
    _fsync_directory(path.parent)


def _read_brain_identity(paths: BrainPaths) -> str | None:
    path = paths.brain_identity_file
    if path.is_symlink():
        raise RecoverySetError(f"Brain identity must not be a symlink: {path}")
    if not path.exists():
        return None
    if not path.is_file():
        raise RecoverySetError(f"Brain identity is not a regular file: {path}")
    value = path.read_text(encoding="utf-8").strip()
    if not BRAIN_ID_PATTERN.fullmatch(value):
        raise RecoverySetError("Brain identity has an invalid format")
    return value


def ensure_brain_identity(paths: BrainPaths) -> str:
    existing = _read_brain_identity(paths)
    if existing is not None:
        return existing
    identity = new_id("brain")
    _write_private_file(paths.brain_identity_file, f"{identity}\n".encode("utf-8"))
    return identity


def _next_recovery_generation(paths: BrainPaths) -> int:
    generation_path = paths.recovery_generation_file
    if generation_path.is_symlink():
        raise RecoverySetError(
            f"recovery generation must not be a symlink: {generation_path}"
        )
    current = 0
    if generation_path.exists():
        if not generation_path.is_file():
            raise RecoverySetError(
                f"recovery generation is not a regular file: {generation_path}"
            )
        try:
            current = int(generation_path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError) as exc:
            raise RecoverySetError("recovery generation is invalid") from exc
        if current < 0:
            raise RecoverySetError("recovery generation cannot be negative")
    generation = current + 1
    _write_private_file(generation_path, f"{generation}\n".encode("ascii"))
    return generation


def _readonly_connection(path: Path, *, immutable: bool) -> sqlite3.Connection:
    uri = f"{path.absolute().as_uri()}?mode=ro"
    if immutable:
        uri += "&immutable=1"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _database_tables(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }


def _database_health(conn: sqlite3.Connection) -> tuple[str, int]:
    integrity_rows = [str(row[0]) for row in conn.execute("PRAGMA integrity_check")]
    if integrity_rows != ["ok"]:
        raise RecoverySetError(
            "database integrity check failed: " + "; ".join(integrity_rows[:10])
        )
    foreign_key_errors = len(conn.execute("PRAGMA foreign_key_check").fetchall())
    if foreign_key_errors:
        raise RecoverySetError(
            f"database foreign-key check found {foreign_key_errors} errors"
        )
    return "ok", foreign_key_errors


def _require_known_migration_prefix(
    actual: tuple[Any, ...],
    expected: tuple[Any, ...],
    *,
    database_name: str,
) -> None:
    """Accept an older schema only when it is an exact known migration prefix."""

    if not actual or len(actual) > len(expected) or actual != expected[: len(actual)]:
        raise RecoverySetError(
            f"{database_name} schema versions {actual} are not a complete prefix "
            f"of known migrations {expected}"
        )


def _schema_summary(
    actual: tuple[Any, ...],
    expected: tuple[Any, ...],
) -> dict[str, Any]:
    actual_version = int(actual[-1][0] if isinstance(actual[-1], tuple) else actual[-1])
    runtime_version = int(
        expected[-1][0] if isinstance(expected[-1], tuple) else expected[-1]
    )
    return {
        "schema_version": actual_version,
        "runtime_schema_version": runtime_version,
        "schema_compatibility": (
            "current" if actual == expected else "known_migration_prefix"
        ),
    }


def _validate_recorded_schema_summary(
    entry: dict[str, Any],
    actual: dict[str, Any],
    *,
    database_name: str,
) -> None:
    if entry.get("schema_version") != actual["schema_version"]:
        raise RecoverySetError(
            "recovery database validation metadata mismatch: "
            f"{database_name}.schema_version"
        )
    recorded_runtime_version = entry.get("runtime_schema_version")
    if (
        isinstance(recorded_runtime_version, bool)
        or not isinstance(recorded_runtime_version, int)
        or recorded_runtime_version < actual["schema_version"]
        or recorded_runtime_version > actual["runtime_schema_version"]
    ):
        raise RecoverySetError(
            "recovery database validation metadata mismatch: "
            f"{database_name}.runtime_schema_version"
        )
    recorded_compatibility = (
        "current"
        if recorded_runtime_version == actual["schema_version"]
        else "known_migration_prefix"
    )
    if entry.get("schema_compatibility") != recorded_compatibility:
        raise RecoverySetError(
            "recovery database validation metadata mismatch: "
            f"{database_name}.schema_compatibility"
        )


def _validate_knowledge_database(path: Path, *, immutable: bool) -> dict[str, Any]:
    _require_private_database_file(path)
    try:
        with _readonly_connection(path, immutable=immutable) as conn:
            tables = _database_tables(conn)
            if not REQUIRED_KNOWLEDGE_TABLES.issubset(tables):
                missing = sorted(REQUIRED_KNOWLEDGE_TABLES - tables)
                raise RecoverySetError(
                    f"knowledge database is missing tables: {', '.join(missing)}"
                )
            versions = tuple(
                int(row[0])
                for row in conn.execute(
                    "SELECT version FROM schema_migrations ORDER BY version"
                )
            )
            expected = tuple(version for version, _name, _migration in MIGRATIONS)
            _require_known_migration_prefix(
                versions,
                expected,
                database_name="knowledge",
            )
            integrity, foreign_key_errors = _database_health(conn)
    except sqlite3.DatabaseError as exc:
        raise RecoverySetError(
            f"knowledge database cannot be validated: {path}"
        ) from exc
    return {
        "schema_versions": list(versions),
        **_schema_summary(versions, expected),
        "integrity_check": integrity,
        "foreign_key_errors": foreign_key_errors,
    }


def _validate_operational_database(path: Path, *, immutable: bool) -> dict[str, Any]:
    _require_private_database_file(path)
    try:
        with _readonly_connection(path, immutable=immutable) as conn:
            tables = _database_tables(conn)
            if not REQUIRED_OPERATIONAL_TABLES.issubset(tables):
                missing = sorted(REQUIRED_OPERATIONAL_TABLES - tables)
                raise RecoverySetError(
                    f"operational database is missing tables: {', '.join(missing)}"
                )
            versions = tuple(
                (int(row[0]), str(row[1]))
                for row in conn.execute(
                    "SELECT version, name FROM ops_schema_migrations ORDER BY version"
                )
            )
            expected = tuple(
                (version, name) for version, name, _migration in OPERATIONAL_MIGRATIONS
            )
            _require_known_migration_prefix(
                versions,
                expected,
                database_name="operational",
            )
            integrity, foreign_key_errors = _database_health(conn)
    except sqlite3.DatabaseError as exc:
        raise RecoverySetError(
            f"operational database cannot be validated: {path}"
        ) from exc
    return {
        "schema_versions": [
            {"version": version, "name": name} for version, name in versions
        ],
        **_schema_summary(versions, expected),
        "integrity_check": integrity,
        "foreign_key_errors": foreign_key_errors,
    }


def _require_private_database_file(path: Path) -> None:
    if path.is_symlink() or not path.is_file():
        raise RecoverySetError(f"database is missing or unsafe: {path}")


def _open_barrier_connection(path: Path) -> sqlite3.Connection:
    uri = f"{path.absolute().as_uri()}?mode=rw"
    conn = sqlite3.connect(uri, uri=True, isolation_level=None)
    conn.execute(f"PRAGMA busy_timeout={RECOVERY_BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def _coordinated_database_barrier(
    paths: BrainPaths,
) -> Iterator[tuple[sqlite3.Connection, sqlite3.Connection]]:
    """Block new SQLite writers in fixed knowledge-then-operations order."""

    with closing(_open_barrier_connection(paths.sqlite_path)) as knowledge:
        with closing(_open_barrier_connection(paths.ops_sqlite_path)) as operations:
            try:
                knowledge.execute("BEGIN IMMEDIATE")
                operations.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as exc:
                if knowledge.in_transaction:
                    knowledge.rollback()
                if operations.in_transaction:
                    operations.rollback()
                raise RecoverySetError(
                    "could not establish the coordinated database write barrier"
                ) from exc
            try:
                yield knowledge, operations
            finally:
                if operations.in_transaction:
                    operations.rollback()
                if knowledge.in_transaction:
                    knowledge.rollback()


def _online_backup(source_path: Path, target_path: Path) -> None:
    if target_path.exists() or target_path.is_symlink():
        raise RecoverySetError(
            f"recovery database target already exists: {target_path}"
        )
    with _readonly_connection(source_path, immutable=False) as source:
        with sqlite3.connect(target_path) as target:
            source.backup(target)
    os.chmod(target_path, RECOVERY_FILE_MODE)
    with target_path.open("rb") as handle:
        os.fsync(handle.fileno())


def _knowledge_watermark(conn: sqlite3.Connection) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT id, finished_at
        FROM ingestion_runs
        WHERE status = 'success'
        ORDER BY COALESCE(finished_at, started_at) DESC, id DESC
        LIMIT 1
        """
    ).fetchone()
    return {
        "last_successful_ingestion_run_id": str(row[0]) if row else None,
        "last_successful_ingestion_at": str(row[1]) if row and row[1] else None,
    }


def _operational_cursor_watermark(conn: sqlite3.Connection) -> dict[str, Any]:
    rows = [
        list(row)
        for row in conn.execute(
            """
            SELECT connector_id, source_type, account_key, stream_key,
                   cursor, watermark, generation, updated_at
            FROM ops_source_cursors
            ORDER BY connector_id, account_key, stream_key
            """
        )
    ]
    encoded = json.dumps(
        rows,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=False,
    ).encode("utf-8")
    return {
        "cursor_count": len(rows),
        "cursor_set_sha256": hashlib.sha256(encoded).hexdigest(),
        "latest_cursor_update_at": max(
            (str(row[7]) for row in rows if row[7]),
            default=None,
        ),
    }


def _primary_epoch(paths: BrainPaths) -> int | None:
    try:
        config = load_sync_config(paths)
    except FileNotFoundError:
        return None
    value = config.raw.get("primary_epoch")
    if value is None:
        return None
    try:
        epoch = int(value)
    except (TypeError, ValueError) as exc:
        raise RecoverySetError("configured primary_epoch is invalid") from exc
    if epoch < 0:
        raise RecoverySetError("configured primary_epoch cannot be negative")
    return epoch


def _safe_recovery_target(paths: BrainPaths, target: Path) -> Path:
    resolved = target.expanduser().resolve()
    db_dir = paths.db_dir.resolve()
    if (
        resolved == paths.home
        or resolved == db_dir
        or resolved.is_relative_to(db_dir)
        or db_dir.is_relative_to(resolved)
    ):
        raise RecoverySetError(
            "recovery output must not overlap the live database home"
        )
    if resolved.exists() or resolved.is_symlink():
        raise RecoverySetError(f"recovery output already exists: {resolved}")
    return resolved


def create_coordinated_recovery_set(
    paths: BrainPaths,
    operational_service: OperationalService,
    *,
    output_dir: Path | None = None,
) -> dict[str, Any]:
    """Create one committed, checksummed database-pair recovery generation."""

    if operational_service.paths.home != paths.home:
        raise RecoverySetError("operational service belongs to a different Brain home")
    # Validate the existing stores read-only before creating identity or generation
    # metadata. In particular, do not initialize or migrate either database here.
    _validate_knowledge_database(paths.sqlite_path, immutable=False)
    _validate_operational_database(paths.ops_sqlite_path, immutable=False)
    # Refuse an unauthorized or lease-less caller and establish the shared Brain
    # identity before hashing canonical source state. Authority is checked again
    # for the actual snapshot.
    with operational_service.mutation_lease():
        brain_id = ensure_brain_identity(paths)
    backup_set_id = new_id("recovery")
    target = _safe_recovery_target(
        paths,
        output_dir or (paths.recovery_root / backup_set_id),
    )
    partial = target.with_name(f".{target.name}.partial-{backup_set_id}")
    if partial.exists() or partial.is_symlink():
        raise RecoverySetError(f"partial recovery output already exists: {partial}")
    if output_dir is None:
        target.parent.mkdir(
            mode=RECOVERY_DIRECTORY_MODE,
            parents=True,
            exist_ok=True,
        )
        os.chmod(target.parent, RECOVERY_DIRECTORY_MODE)
    partial.mkdir(mode=RECOVERY_DIRECTORY_MODE, parents=False, exist_ok=False)
    os.chmod(partial, RECOVERY_DIRECTORY_MODE)
    with operational_service.mutation_lease() as authority:
        if ensure_brain_identity(paths) != brain_id:
            raise RecoverySetError("Brain identity changed during recovery creation")
        source_manifest = canonical_manifest_hash(paths.home)
        generation = _next_recovery_generation(paths)
        created_at = now_iso()
        primary_epoch = _primary_epoch(paths)
        with _coordinated_database_barrier(paths) as (knowledge, operations):
            watermarks = {
                "source_manifest_sha256": source_manifest,
                **_knowledge_watermark(knowledge),
                **_operational_cursor_watermark(operations),
            }
            _online_backup(
                paths.sqlite_path,
                partial / KNOWLEDGE_DATABASE_FILENAME,
            )
            _online_backup(
                paths.ops_sqlite_path,
                partial / OPERATIONAL_DATABASE_FILENAME,
            )

    knowledge_path = partial / KNOWLEDGE_DATABASE_FILENAME
    operations_path = partial / OPERATIONAL_DATABASE_FILENAME
    knowledge_validation = _validate_knowledge_database(
        knowledge_path,
        immutable=True,
    )
    operational_validation = _validate_operational_database(
        operations_path,
        immutable=True,
    )
    completed_at = now_iso()
    manifest = {
        "format_version": RECOVERY_FORMAT_VERSION,
        "artifact_kind": "database_pair",
        "status": "complete",
        "backup_set_id": backup_set_id,
        "generation": generation,
        "created_at": created_at,
        "completed_at": completed_at,
        "consistency": "sqlite_write_barrier",
        "brain_id": brain_id,
        "primary_node_id": authority.node_id,
        "role": authority.role,
        "primary_epoch": primary_epoch,
        "runtime_version": _package_version(),
        "sqlite_version": sqlite3.sqlite_version,
        "schema_metadata_version": SCHEMA_METADATA_VERSION,
        "scope": recovery_scope(),
        "watermarks": watermarks,
        "protection": {
            "at_rest": "owner_only_filesystem_permissions",
            "transport": "not_exported",
        },
        "databases": {
            "knowledge": {
                "filename": KNOWLEDGE_DATABASE_FILENAME,
                "generation": generation,
                "bytes": knowledge_path.stat().st_size,
                "sha256": file_sha256(knowledge_path),
                **knowledge_validation,
            },
            "operations": {
                "filename": OPERATIONAL_DATABASE_FILENAME,
                "generation": generation,
                "bytes": operations_path.stat().st_size,
                "sha256": file_sha256(operations_path),
                **operational_validation,
            },
        },
    }
    manifest_bytes = (
        json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    ).encode("utf-8")
    manifest_path = partial / MANIFEST_FILENAME
    _write_private_file(manifest_path, manifest_bytes)
    manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
    _write_private_file(
        partial / COMMITTED_FILENAME,
        f"{manifest_sha256}\n".encode("ascii"),
    )
    _fsync_directory(partial)
    if target.exists() or target.is_symlink():
        raise RecoverySetError("recovery output appeared during recovery creation")
    os.replace(partial, target)
    _fsync_directory(target.parent)
    verification = verify_recovery_set(target)
    return {
        "status": "ok",
        "path": str(target),
        "backup_set_id": backup_set_id,
        "generation": generation,
        "manifest_sha256": verification["manifest_sha256"],
        "databases": verification["manifest"]["databases"],
        "scope": recovery_scope(),
    }


def _read_recovery_manifest(recovery_dir: Path) -> tuple[dict[str, Any], str]:
    candidate = recovery_dir.expanduser()
    if candidate.is_symlink():
        raise RecoverySetError(f"recovery set is missing or unsafe: {candidate}")
    root = candidate.resolve()
    if not root.is_dir():
        raise RecoverySetError(f"recovery set is missing or unsafe: {root}")
    if stat.S_IMODE(root.stat().st_mode) & 0o077:
        raise RecoverySetError("recovery set directory is not owner-only")
    manifest_path = root / MANIFEST_FILENAME
    committed_path = root / COMMITTED_FILENAME
    for path in (manifest_path, committed_path):
        if path.is_symlink() or not path.is_file():
            raise RecoverySetError(f"recovery set member is missing or unsafe: {path}")
        if stat.S_IMODE(path.stat().st_mode) & 0o077:
            raise RecoverySetError(
                f"recovery set member is not owner-only: {path.name}"
            )
    manifest_bytes = manifest_path.read_bytes()
    manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
    if committed_path.read_text(encoding="ascii").strip() != manifest_sha256:
        raise RecoverySetError("recovery COMMITTED marker does not match the manifest")
    try:
        manifest = json.loads(manifest_bytes)
    except json.JSONDecodeError as exc:
        raise RecoverySetError("recovery manifest is not valid JSON") from exc
    if not isinstance(manifest, dict):
        raise RecoverySetError("recovery manifest must be an object")
    return manifest, manifest_sha256


def verify_recovery_set(recovery_dir: Path) -> dict[str, Any]:
    """Verify publication, hashes, generations, schemas, and SQLite integrity."""

    manifest, manifest_sha256 = _read_recovery_manifest(recovery_dir)
    root = recovery_dir.expanduser().resolve()
    if manifest.get("format_version") != RECOVERY_FORMAT_VERSION:
        raise RecoverySetError("unsupported recovery format version")
    if manifest.get("artifact_kind") != "database_pair":
        raise RecoverySetError("recovery artifact is not a database pair")
    if manifest.get("status") != "complete":
        raise RecoverySetError("recovery manifest is incomplete")
    schema_metadata_version = manifest.get("schema_metadata_version")
    if schema_metadata_version not in {None, SCHEMA_METADATA_VERSION}:
        raise RecoverySetError("unsupported recovery schema metadata version")
    manifest_scope = manifest.get("scope")
    if manifest_scope is not None and manifest_scope != recovery_scope():
        raise RecoverySetError("recovery manifest has an invalid scope declaration")
    if not BACKUP_SET_ID_PATTERN.fullmatch(str(manifest.get("backup_set_id") or "")):
        raise RecoverySetError("recovery manifest has an invalid backup-set identity")
    if not BRAIN_ID_PATTERN.fullmatch(str(manifest.get("brain_id") or "")):
        raise RecoverySetError("recovery manifest has an invalid Brain identity")
    generation = manifest.get("generation")
    if (
        isinstance(generation, bool)
        or not isinstance(generation, int)
        or generation < 1
    ):
        raise RecoverySetError("recovery generation is invalid")
    databases = manifest.get("databases")
    if not isinstance(databases, dict) or set(databases) != {
        "knowledge",
        "operations",
    }:
        raise RecoverySetError("recovery manifest must contain exactly two databases")
    expected = {
        "knowledge": KNOWLEDGE_DATABASE_FILENAME,
        "operations": OPERATIONAL_DATABASE_FILENAME,
    }
    validation: dict[str, Any] = {}
    for name, filename in expected.items():
        entry = databases.get(name)
        if not isinstance(entry, dict):
            raise RecoverySetError(f"recovery database entry is invalid: {name}")
        if entry.get("filename") != filename or entry.get("generation") != generation:
            raise RecoverySetError(f"recovery database generation is mixed: {name}")
        path = root / filename
        _require_private_database_file(path)
        if stat.S_IMODE(path.stat().st_mode) & 0o077:
            raise RecoverySetError(f"recovery database is not owner-only: {name}")
        if entry.get("bytes") != path.stat().st_size:
            raise RecoverySetError(f"recovery database size mismatch: {name}")
        if entry.get("sha256") != file_sha256(path):
            raise RecoverySetError(f"recovery database checksum mismatch: {name}")
        actual = (
            _validate_knowledge_database(path, immutable=True)
            if name == "knowledge"
            else _validate_operational_database(path, immutable=True)
        )
        validation_keys = [
            "schema_versions",
            "integrity_check",
            "foreign_key_errors",
        ]
        for key in validation_keys:
            if entry.get(key) != actual[key]:
                raise RecoverySetError(
                    f"recovery database validation metadata mismatch: {name}.{key}"
                )
        if schema_metadata_version == SCHEMA_METADATA_VERSION:
            _validate_recorded_schema_summary(
                entry,
                actual,
                database_name=name,
            )
        validation[name] = actual
    return {
        "status": "ok",
        "path": str(root),
        "manifest_sha256": manifest_sha256,
        "manifest": manifest,
        "validation": validation,
        "scope": recovery_scope(),
    }


def _restore_database(
    source_path: Path,
    target_path: Path,
    *,
    expected_bytes: int,
    expected_sha256: str,
) -> None:
    if source_path.is_symlink() or not source_path.is_file():
        raise RecoverySetError(f"restore source is missing or unsafe: {source_path}")
    if (
        source_path.stat().st_size != expected_bytes
        or file_sha256(source_path) != expected_sha256
    ):
        raise RecoverySetError("restore source changed after recovery verification")
    target_path.parent.mkdir(mode=RECOVERY_DIRECTORY_MODE, parents=True, exist_ok=True)
    with _readonly_connection(source_path, immutable=True) as source:
        with sqlite3.connect(target_path) as target:
            source.backup(target)
    if (
        target_path.stat().st_size != expected_bytes
        or file_sha256(target_path) != expected_sha256
    ):
        raise RecoverySetError(
            "restored database content does not match the recovery manifest"
        )
    with sqlite3.connect(target_path) as target:
        target.execute("PRAGMA journal_mode=WAL")
    os.chmod(target_path, RECOVERY_FILE_MODE)
    with target_path.open("rb") as handle:
        os.fsync(handle.fileno())


def restore_recovery_set_isolated(
    recovery_dir: Path,
    target_home: Path,
) -> dict[str, Any]:
    """Restore a verified pair into a new home; never replace a live home."""

    verification = verify_recovery_set(recovery_dir)
    manifest = verification["manifest"]
    target_candidate = target_home.expanduser()
    if target_candidate.is_symlink():
        raise RecoverySetError("isolated restore target must not be a symlink")
    target = target_candidate.resolve()
    source = recovery_dir.expanduser().resolve()
    if target.exists() or target.is_symlink():
        raise RecoverySetError("isolated restore target must not already exist")
    if (
        target == source
        or target.is_relative_to(source)
        or source.is_relative_to(target)
    ):
        raise RecoverySetError("restore target must not overlap the recovery set")
    staging = target.with_name(
        f".{target.name}.restore-partial-{manifest['backup_set_id']}"
    )
    if staging.exists() or staging.is_symlink():
        raise RecoverySetError(f"restore staging path already exists: {staging}")
    staging.mkdir(mode=RECOVERY_DIRECTORY_MODE, parents=False, exist_ok=False)
    os.chmod(staging, RECOVERY_DIRECTORY_MODE)
    staged_paths = BrainPaths.from_value(staging)
    knowledge_entry = manifest["databases"]["knowledge"]
    operational_entry = manifest["databases"]["operations"]
    _restore_database(
        source / KNOWLEDGE_DATABASE_FILENAME,
        staged_paths.sqlite_path,
        expected_bytes=knowledge_entry["bytes"],
        expected_sha256=knowledge_entry["sha256"],
    )
    _restore_database(
        source / OPERATIONAL_DATABASE_FILENAME,
        staged_paths.ops_sqlite_path,
        expected_bytes=operational_entry["bytes"],
        expected_sha256=operational_entry["sha256"],
    )
    final_source_verification = verify_recovery_set(recovery_dir)
    if final_source_verification["manifest_sha256"] != verification["manifest_sha256"]:
        raise RecoverySetError("recovery manifest changed during isolated restore")
    _validate_knowledge_database(staged_paths.sqlite_path, immutable=True)
    _validate_operational_database(staged_paths.ops_sqlite_path, immutable=True)
    _write_private_file(
        staged_paths.brain_identity_file,
        f"{manifest['brain_id']}\n".encode("utf-8"),
    )
    _write_private_file(
        staged_paths.recovery_generation_file,
        f"{manifest['generation']}\n".encode("ascii"),
    )
    restored_from = {
        "status": "quarantined",
        "activation_required": True,
        "backup_set_id": manifest["backup_set_id"],
        "generation": manifest["generation"],
        "manifest_sha256": verification["manifest_sha256"],
        "restored_at": now_iso(),
        "daemon_started": False,
    }
    _write_private_file(
        staged_paths.restore_quarantine_file,
        (json.dumps(restored_from, indent=2, sort_keys=True) + "\n").encode("utf-8"),
    )
    for directory in (
        staged_paths.db_dir,
        staged_paths.config,
        staged_paths.config_local,
        staged_paths.config_shared,
        staging,
    ):
        os.chmod(directory, RECOVERY_DIRECTORY_MODE)
        _fsync_directory(directory)
    if target.exists() or target.is_symlink():
        raise RecoverySetError("isolated restore target appeared during restore")
    os.replace(staging, target)
    _fsync_directory(target.parent)
    restored_paths = BrainPaths.from_value(target)
    _validate_knowledge_database(restored_paths.sqlite_path, immutable=True)
    _validate_operational_database(restored_paths.ops_sqlite_path, immutable=True)
    return {
        "status": "ok",
        "path": str(target),
        "backup_set_id": manifest["backup_set_id"],
        "generation": manifest["generation"],
        "brain_id": manifest["brain_id"],
        "daemon_started": False,
        "scope": recovery_scope(),
    }
