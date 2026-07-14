from __future__ import annotations

import multiprocessing
import threading
import time
from pathlib import Path

import pytest

from pkm_brain.operational_service import (
    OperationalService,
    OperationalWriteRefusedError,
)
from pkm_brain.operational_state import OperationalObservation, get_source_cursor
from pkm_brain.paths import BrainPaths
from pkm_brain.sync_config import SyncConfig, write_sync_config
from pkm_brain.sync_setup import init_primary, init_secondary


def calendar_observation() -> OperationalObservation:
    return OperationalObservation(
        source_type="google_calendar",
        account_key="account-1",
        stream_key="primary",
        source_key="event-42",
        source_revision="etag-1",
        source_order=1,
        source_updated_at="2026-07-13T15:00:00+00:00",
        item_kind="event",
        title="Project review",
        observed_at="2026-07-13T15:00:30+00:00",
        starts_at="2026-07-14T17:00:00+00:00",
        ends_at="2026-07-14T17:30:00+00:00",
        source_timezone="America/Los_Angeles",
        evidence_refs=({"calendar_id": "primary", "event_id": "event-42"},),
    )


def leased_service(paths: BrainPaths) -> OperationalService:
    return OperationalService(paths, writer_guard=lambda: None)


def hold_process_mutation_lease(
    home: str,
    entered: multiprocessing.synchronize.Event,
    release: multiprocessing.synchronize.Event,
) -> None:
    paths = BrainPaths.from_value(home)
    with leased_service(paths).mutation_lease():
        entered.set()
        release.wait(timeout=10)


def test_implicit_single_can_initialize_and_mutate(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    service = leased_service(paths)

    assert service.authority_status().role == "single"
    service.initialize()
    result = service.reconcile_observation(calendar_observation())
    cursor = service.save_source_cursor(
        "calendar",
        "account-1",
        "primary",
        source_type="google_calendar",
        cursor="cursor-1",
    )

    assert result.state == "active"
    assert cursor["cursor"] == "cursor-1"
    assert get_source_cursor(
        paths.ops_sqlite_path,
        "calendar",
        "account-1",
        "primary",
    ) == cursor


def test_matching_configured_primary_can_mutate(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    init_primary(paths, "primary-node", force=True)
    service = leased_service(paths)

    service.initialize()

    assert service.authority_status().can_write is True
    assert paths.ops_sqlite_path.exists()


def test_secondary_rejects_every_write_before_creating_store(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    init_secondary(
        paths,
        "secondary-node",
        primary_node_id="primary-node",
        outbox_path=paths.outbox / "secondary-node",
        force=True,
    )
    service = leased_service(paths)

    operations = (
        service.initialize,
        lambda: service.reconcile_observation(calendar_observation()),
        lambda: service.reconcile_source_unit([calendar_observation()]),
        lambda: service.record_item_feedback("item-1", "done"),
        lambda: service.save_source_cursor(
            "calendar",
            "account-1",
            "primary",
            source_type="google_calendar",
            cursor="cursor-1",
        ),
    )
    for operation in operations:
        with pytest.raises(OperationalWriteRefusedError, match="secondary_is_read_only"):
            operation()

    assert not paths.ops_sqlite_path.exists()
    assert not paths.operational_writer_lock_file.exists()


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        ("invalid", "invalid_sync_config"),
        ("home", "configured_home_mismatch"),
        ("missing_node", "local_node_identity_missing"),
        ("wrong_node", "local_node_identity_mismatch"),
    ],
)
def test_invalid_configured_authority_fails_closed(
    tmp_path: Path,
    mutate: str,
    reason: str,
) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    init_primary(paths, "primary-node", force=True)
    if mutate == "invalid":
        paths.sync_config_file.write_text("role: [invalid\n", encoding="utf-8")
    elif mutate == "home":
        config = SyncConfig.from_dict(
            {
                "node_id": "primary-node",
                "role": "primary",
                "brain_home": str(tmp_path / "another-brain"),
                "peers": [],
            },
            paths.home,
        )
        write_sync_config(paths, config)
    elif mutate == "missing_node":
        paths.local_node_id_file.unlink()
    else:
        paths.local_node_id_file.write_text("another-node\n", encoding="utf-8")

    service = leased_service(paths)
    with pytest.raises(OperationalWriteRefusedError, match=reason):
        service.initialize()
    assert not paths.ops_sqlite_path.exists()


def test_authority_is_rechecked_after_service_construction(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    init_primary(paths, "node-1", force=True)
    service = leased_service(paths)
    service.initialize()

    init_secondary(
        paths,
        "node-1",
        primary_node_id="another-primary",
        outbox_path=paths.outbox / "node-1",
        force=True,
    )

    with pytest.raises(OperationalWriteRefusedError, match="secondary_is_read_only"):
        service.save_source_cursor(
            "calendar",
            "account-1",
            "primary",
            source_type="google_calendar",
            cursor="cursor-after-demotion",
        )


@pytest.mark.parametrize("variant", ["deleted", "dangling_symlink"])
def test_missing_sync_config_on_configured_node_fails_closed(
    tmp_path: Path,
    variant: str,
) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    init_primary(paths, "primary-node", force=True)
    paths.sync_config_file.unlink()
    if variant == "dangling_symlink":
        paths.sync_config_file.symlink_to(paths.config / "missing-sync.yaml")

    with pytest.raises(OperationalWriteRefusedError, match="sync_config"):
        leased_service(paths).initialize()

    assert not paths.ops_sqlite_path.exists()


def test_service_without_daemon_writer_guard_is_read_only(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")

    with pytest.raises(OperationalWriteRefusedError, match="daemon writer lease"):
        OperationalService(paths).initialize()

    assert not paths.ops_sqlite_path.exists()


def test_services_for_one_home_share_mutation_serialization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    first = leased_service(paths)
    second = leased_service(paths)
    first.initialize()
    active = 0
    max_active = 0
    guard = threading.Lock()

    def fake_save(*_args: object, **_kwargs: object) -> dict[str, object]:
        nonlocal active, max_active
        with guard:
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.05)
        with guard:
            active -= 1
        return {"ok": True}

    monkeypatch.setattr(
        "pkm_brain.operational_service.save_source_cursor",
        fake_save,
    )
    threads = [
        threading.Thread(
            target=service.save_source_cursor,
            args=("calendar", "account-1", "primary"),
            kwargs={"source_type": "google_calendar", "cursor": str(index)},
        )
        for index, service in enumerate((first, second))
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2)

    assert all(not thread.is_alive() for thread in threads)
    assert max_active == 1


def test_operational_writer_lease_serializes_across_processes(
    tmp_path: Path,
) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    context = multiprocessing.get_context("spawn")
    child_entered = context.Event()
    release_child = context.Event()
    child = context.Process(
        target=hold_process_mutation_lease,
        args=(str(paths.home), child_entered, release_child),
    )
    child.start()
    assert child_entered.wait(timeout=5)

    parent_entered = threading.Event()

    def enter_parent_lease() -> None:
        with leased_service(paths).mutation_lease():
            parent_entered.set()

    parent = threading.Thread(target=enter_parent_lease)
    parent.start()
    assert not parent_entered.wait(timeout=0.2)
    release_child.set()
    assert parent_entered.wait(timeout=5)
    parent.join(timeout=2)
    child.join(timeout=5)

    assert not parent.is_alive()
    assert child.exitcode == 0
