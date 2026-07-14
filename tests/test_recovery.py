from __future__ import annotations

import hashlib
import json
import sqlite3
import stat
from pathlib import Path

import pytest

import pkm_brain.recovery as recovery_module
from pkm_brain.daemon import BrainDaemon
from pkm_brain.db import connection, init_db
from pkm_brain.operational_db import init_operational_db, operational_connection
from pkm_brain.operational_service import (
    OperationalService,
    OperationalWriteRefusedError,
)
from pkm_brain.operational_state import (
    OperationalObservation,
    get_item,
    get_source_cursor,
)
from pkm_brain.paths import BrainPaths
from pkm_brain.recovery import (
    COMMITTED_FILENAME,
    MANIFEST_FILENAME,
    RecoverySetError,
    create_coordinated_recovery_set,
    restore_recovery_set_isolated,
    verify_recovery_set,
)
from pkm_brain.sync_setup import init_secondary
from pkm_brain.sync_status import canonical_manifest_hash


def service(paths: BrainPaths) -> OperationalService:
    return OperationalService(paths, writer_guard=lambda: None)


def observation() -> OperationalObservation:
    return OperationalObservation(
        source_type="google_calendar",
        account_key="account-1",
        stream_key="primary",
        source_key="event-42",
        source_revision="etag-1",
        source_order=1,
        source_updated_at="2026-07-13T15:00:00+00:00",
        item_kind="event",
        title="Recovery review",
        observed_at="2026-07-13T15:00:30+00:00",
        starts_at="2026-07-14T17:00:00+00:00",
        ends_at="2026-07-14T17:30:00+00:00",
        evidence_refs=({"calendar_id": "primary", "event_id": "event-42"},),
    )


def prepared_home(tmp_path: Path) -> tuple[BrainPaths, OperationalService, str]:
    paths = BrainPaths.from_value(tmp_path / "brain")
    init_db(paths.sqlite_path)
    operational = service(paths)
    operational.initialize()
    with connection(paths.sqlite_path) as conn:
        conn.execute(
            """
            INSERT INTO memories(
              id, memory_type, scope, content, confidence, source_ids,
              status, created_at, updated_at
            ) VALUES (
              'memory-reviewed', 'decision', 'global', 'Preserve this review',
              1.0, '[]', 'active',
              '2026-07-13T00:00:00+00:00', '2026-07-13T00:00:00+00:00'
            )
            """
        )
    result = operational.reconcile_observation(observation())
    operational.record_item_feedback(
        result.item_id,
        "confirm",
        idempotency_key="confirm-recovery-item",
    )
    operational.save_source_cursor(
        "calendar",
        "account-1",
        "primary",
        source_type="google_calendar",
        cursor="private-provider-cursor",
        watermark="2026-07-13T15:00:00+00:00",
    )
    return paths, operational, result.item_id


def test_coordinated_recovery_captures_wal_pair_and_private_manifest(
    tmp_path: Path,
) -> None:
    paths, operational, item_id = prepared_home(tmp_path)
    knowledge_writer = sqlite3.connect(paths.sqlite_path)
    operations_writer = sqlite3.connect(paths.ops_sqlite_path)
    try:
        knowledge_writer.execute(
            "UPDATE memories SET content = 'Committed knowledge WAL state' "
            "WHERE id = 'memory-reviewed'"
        )
        knowledge_writer.commit()
        operations_writer.execute(
            "UPDATE ops_items SET title = 'Committed operations WAL state' WHERE id = ?",
            (item_id,),
        )
        operations_writer.commit()

        result = create_coordinated_recovery_set(
            paths,
            operational,
            output_dir=tmp_path / "recovery-set",
        )
    finally:
        knowledge_writer.close()
        operations_writer.close()

    recovery_dir = Path(result["path"])
    verification = verify_recovery_set(recovery_dir)
    manifest = verification["manifest"]
    assert manifest["artifact_kind"] == "database_pair"
    assert manifest["consistency"] == "sqlite_write_barrier"
    assert manifest["databases"]["knowledge"]["generation"] == result["generation"]
    assert manifest["databases"]["operations"]["generation"] == result["generation"]
    assert manifest["watermarks"]["source_manifest_sha256"] == canonical_manifest_hash(
        paths.home
    )
    assert "private-provider-cursor" not in json.dumps(manifest)
    assert stat.S_IMODE(recovery_dir.stat().st_mode) == 0o700
    for name in ("brain.sqlite", "ops.sqlite", MANIFEST_FILENAME, COMMITTED_FILENAME):
        assert stat.S_IMODE((recovery_dir / name).stat().st_mode) == 0o600
    with sqlite3.connect(recovery_dir / "brain.sqlite") as conn:
        assert conn.execute(
            "SELECT content FROM memories WHERE id = 'memory-reviewed'"
        ).fetchone()[0] == "Committed knowledge WAL state"
    with sqlite3.connect(recovery_dir / "ops.sqlite") as conn:
        assert conn.execute(
            "SELECT title FROM ops_items WHERE id = ?", (item_id,)
        ).fetchone()[0] == "Committed operations WAL state"


def test_recovery_verification_rejects_tampering_and_incomplete_publication(
    tmp_path: Path,
) -> None:
    paths, operational, _item_id = prepared_home(tmp_path)
    result = create_coordinated_recovery_set(
        paths,
        operational,
        output_dir=tmp_path / "recovery-set",
    )
    recovery_dir = Path(result["path"])
    committed = recovery_dir / COMMITTED_FILENAME
    committed.write_text("0" * 64 + "\n", encoding="ascii")

    with pytest.raises(RecoverySetError, match="COMMITTED"):
        verify_recovery_set(recovery_dir)

    manifest_bytes = (recovery_dir / MANIFEST_FILENAME).read_bytes()
    committed.write_text(
        hashlib.sha256(manifest_bytes).hexdigest() + "\n",
        encoding="ascii",
    )
    with (recovery_dir / "ops.sqlite").open("ab") as handle:
        handle.write(b"tamper")
    with pytest.raises(RecoverySetError, match="size mismatch"):
        verify_recovery_set(recovery_dir)


def test_isolated_restore_preserves_reviewed_and_operational_state(
    tmp_path: Path,
) -> None:
    paths, operational, item_id = prepared_home(tmp_path)
    result = create_coordinated_recovery_set(
        paths,
        operational,
        output_dir=tmp_path / "recovery-set",
    )
    with connection(paths.sqlite_path) as conn:
        conn.execute("DELETE FROM memories WHERE id = 'memory-reviewed'")
    operational.record_item_feedback(
        item_id,
        "dismiss",
        idempotency_key="post-backup-dismiss",
    )

    restored_home = tmp_path / "restored-brain"
    restored = restore_recovery_set_isolated(Path(result["path"]), restored_home)
    restored_paths = BrainPaths.from_value(restored_home)

    assert restored["daemon_started"] is False
    with connection(restored_paths.sqlite_path) as conn:
        assert conn.execute(
            "SELECT content FROM memories WHERE id = 'memory-reviewed'"
        ).fetchone()[0] == "Preserve this review"
    item = get_item(restored_paths.ops_sqlite_path, item_id)
    assert item is not None
    assert item["state"] == "active"
    assert item["human_confirmed_at"] is not None
    cursor = get_source_cursor(
        restored_paths.ops_sqlite_path,
        "calendar",
        "account-1",
        "primary",
    )
    assert cursor is not None
    assert cursor["cursor"] == "private-provider-cursor"
    with operational_connection(restored_paths.ops_sqlite_path):
        pass
    assert restored_paths.brain_identity_file.read_text(encoding="utf-8").strip() == restored["brain_id"]
    assert restored_paths.restore_quarantine_file.exists()
    with pytest.raises(
        OperationalWriteRefusedError,
        match="restored_home_requires_activation",
    ):
        service(restored_paths).save_source_cursor(
            "calendar",
            "account-1",
            "primary",
            source_type="google_calendar",
            cursor="unsafe-second-writer",
        )
    with pytest.raises(RuntimeError, match="quarantined"):
        BrainDaemon(restored_paths, start_scheduler=False).start()


def test_recovery_refuses_secondary_and_existing_restore_target(
    tmp_path: Path,
) -> None:
    paths, operational, _item_id = prepared_home(tmp_path)
    result = create_coordinated_recovery_set(
        paths,
        operational,
        output_dir=tmp_path / "recovery-set",
    )
    existing_target = tmp_path / "existing-target"
    existing_target.mkdir()
    with pytest.raises(RecoverySetError, match="must not already exist"):
        restore_recovery_set_isolated(Path(result["path"]), existing_target)

    secondary = BrainPaths.from_value(tmp_path / "secondary")
    init_secondary(
        secondary,
        "secondary-node",
        primary_node_id="primary-node",
        outbox_path=secondary.outbox / "secondary-node",
        force=True,
    )
    init_operational_db(secondary.ops_sqlite_path)
    with pytest.raises(OperationalWriteRefusedError, match="secondary_is_read_only"):
        create_coordinated_recovery_set(
            secondary,
            service(secondary),
            output_dir=tmp_path / "secondary-recovery",
        )
    assert not (tmp_path / "secondary-recovery").exists()


def test_restore_rejects_mixed_generation_manifest(tmp_path: Path) -> None:
    paths, operational, _item_id = prepared_home(tmp_path)
    result = create_coordinated_recovery_set(
        paths,
        operational,
        output_dir=tmp_path / "recovery-set",
    )
    recovery_dir = Path(result["path"])
    manifest_path = recovery_dir / MANIFEST_FILENAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["databases"]["operations"]["generation"] += 1
    manifest_bytes = (
        json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    ).encode("utf-8")
    manifest_path.write_bytes(manifest_bytes)
    (recovery_dir / COMMITTED_FILENAME).write_text(
        hashlib.sha256(manifest_bytes).hexdigest() + "\n",
        encoding="ascii",
    )

    with pytest.raises(RecoverySetError, match="generation is mixed"):
        verify_recovery_set(recovery_dir)


def test_recovery_root_symlink_is_rejected(tmp_path: Path) -> None:
    paths, operational, _item_id = prepared_home(tmp_path)
    result = create_coordinated_recovery_set(
        paths,
        operational,
        output_dir=tmp_path / "recovery-set",
    )
    link = tmp_path / "recovery-link"
    link.symlink_to(Path(result["path"]), target_is_directory=True)

    with pytest.raises(RecoverySetError, match="missing or unsafe"):
        verify_recovery_set(link)


def test_restore_rejects_source_changed_after_verification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, operational, _item_id = prepared_home(tmp_path)
    result = create_coordinated_recovery_set(
        paths,
        operational,
        output_dir=tmp_path / "recovery-set",
    )
    original = recovery_module._restore_database
    changed = False

    def mutate_then_restore(
        source_path: Path,
        target_path: Path,
        *,
        expected_bytes: int,
        expected_sha256: str,
    ) -> None:
        nonlocal changed
        if not changed:
            changed = True
            with source_path.open("ab") as handle:
                handle.write(b"changed-after-verification")
        original(
            source_path,
            target_path,
            expected_bytes=expected_bytes,
            expected_sha256=expected_sha256,
        )

    monkeypatch.setattr(recovery_module, "_restore_database", mutate_then_restore)
    with pytest.raises(RecoverySetError, match="changed after recovery verification"):
        restore_recovery_set_isolated(
            Path(result["path"]),
            tmp_path / "unsafe-restore",
        )


def test_recovery_output_never_overlaps_live_database_home(tmp_path: Path) -> None:
    paths, operational, _item_id = prepared_home(tmp_path)

    with pytest.raises(RecoverySetError, match="must not overlap"):
        create_coordinated_recovery_set(
            paths,
            operational,
            output_dir=paths.db_dir / "unsafe-backup",
        )

    assert not (paths.db_dir / "unsafe-backup").exists()
