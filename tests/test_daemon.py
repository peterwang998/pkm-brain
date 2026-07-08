from __future__ import annotations

import http.client
import json
import stat
import sys
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

import pytest

from pkm_brain.daemon import (
    BrainDaemon,
    SchedulerJob,
    SerialJobScheduler,
    daemon_handshake_path,
    scheduler_config_path,
)
from pkm_brain.paths import BrainPaths
from pkm_brain.sync_config import PeerConfig, PrimaryConfig, SyncConfig, write_sync_config
from pkm_brain.sync_setup import init_secondary


@contextmanager
def running_daemon(
    paths: BrainPaths,
    *,
    serve_web: bool = False,
    start_scheduler: bool = False,
) -> Iterator[tuple[BrainDaemon, str, int, str]]:
    runtime = BrainDaemon(paths, serve_web=serve_web, start_scheduler=start_scheduler)
    runtime.start()
    assert runtime.server is not None
    thread = threading.Thread(target=runtime.server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = runtime.server.server_address
        yield runtime, str(host), int(port), runtime.token
    finally:
        runtime.server.shutdown()
        thread.join(timeout=2)
        runtime.close()


def request_json(
    host: str,
    port: int,
    method: str,
    path: str,
    *,
    token: str | None = None,
    payload: dict[str, object] | None = None,
) -> tuple[int, dict[str, object]]:
    body = json.dumps(payload or {}).encode("utf-8") if payload is not None else None
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    if body is not None:
        headers["Content-Type"] = "application/json"
    conn = http.client.HTTPConnection(host, port, timeout=5)
    conn.request(method, path, body=body, headers=headers)
    response = conn.getresponse()
    raw = response.read().decode("utf-8")
    conn.close()
    return response.status, json.loads(raw)


def request_text(host: str, port: int, path: str) -> tuple[int, str]:
    conn = http.client.HTTPConnection(host, port, timeout=5)
    conn.request("GET", path)
    response = conn.getresponse()
    body = response.read().decode("utf-8")
    conn.close()
    return response.status, body


def test_daemon_boot_handshake_lock_health_and_shutdown(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")

    with running_daemon(paths) as (_runtime, host, port, token):
        handshake = daemon_handshake_path(paths)
        payload = json.loads(handshake.read_text(encoding="utf-8"))

        assert stat.S_IMODE(handshake.stat().st_mode) == 0o600
        assert payload["port"] == port
        assert payload["token"] == token
        assert payload["home"] == str(paths.home)

        second = BrainDaemon(paths)
        with pytest.raises(RuntimeError, match="already running"):
            second.start()

        status, body = request_json(host, port, "GET", "/api/health")
        assert status == 401
        assert "token" in str(body["error"])

        status, body = request_json(host, port, "GET", "/api/health", token=token)
        assert status == 200
        assert body["ok"] is True
        assert body["port"] == port
        assert body["home"] == str(paths.home)

        status, body = request_json(host, port, "GET", "/api/version", token=token)
        assert status == 200
        assert body["version"]

        status, body = request_json(host, port, "POST", "/api/shutdown", token=token)
        assert status == 200
        assert body["shutting_down"] is True

    assert not daemon_handshake_path(paths).exists()


def test_daemon_health_does_not_load_sentence_transformers(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    sys.modules.pop("sentence_transformers", None)

    with running_daemon(paths) as (_runtime, host, port, token):
        started = time.perf_counter()
        status, body = request_json(host, port, "GET", "/api/health", token=token)
        elapsed = time.perf_counter() - started

    assert status == 200
    assert body["ok"] is True
    assert elapsed < 0.05
    assert "sentence_transformers" not in sys.modules


def test_daemon_static_ui_is_gated_by_serve_web(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")

    with running_daemon(paths, serve_web=False) as (_runtime, host, port, _token):
        status, body = request_text(host, port, "/")
    assert status == 404
    assert "not found" in body

    with running_daemon(paths, serve_web=True) as (_runtime, host, port, _token):
        status, body = request_text(host, port, "/")
    assert status == 200
    assert 'id="token-dialog"' in body


def test_scheduler_due_math_and_serial_executor(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    current = {"now": now}
    active = 0
    max_active = 0
    completed: list[str] = []
    lock = threading.Lock()

    def clock() -> datetime:
        return current["now"]

    def handler(name: str) -> dict[str, object]:
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.05)
        with lock:
            active -= 1
            completed.append(name)
        return {"status": "success", "name": name}

    scheduler = SerialJobScheduler(
        paths,
        tick_seconds=60,
        now=clock,
        jobs=[
            SchedulerJob("a", 10, lambda: handler("a")),
            SchedulerJob("b", 10, lambda: handler("b")),
        ],
    )
    assert scheduler.enqueue_due_jobs() == []
    current["now"] = now + timedelta(seconds=10)
    assert scheduler.enqueue_due_jobs() == ["a", "b"]

    scheduler.start()
    try:
        deadline = time.time() + 3
        while len(completed) < 2 and time.time() < deadline:
            time.sleep(0.01)
    finally:
        scheduler.stop()

    assert sorted(completed) == ["a", "b"]
    assert max_active == 1


def test_scheduler_pause_persists(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    scheduler = SerialJobScheduler(paths, jobs=[SchedulerJob("job", 60, lambda: {"status": "success"})])

    state = scheduler.pause(120)
    assert state["paused_until"]
    assert scheduler_config_path(paths).exists()

    reloaded = SerialJobScheduler(paths, jobs=[SchedulerJob("job", 60, lambda: {"status": "success"})])
    assert reloaded.as_dict()["paused_until"] == state["paused_until"]

    resumed = reloaded.resume()
    assert resumed["paused_until"] is None


def test_scheduler_registry_is_role_aware(tmp_path: Path) -> None:
    secondary = BrainPaths.from_value(tmp_path / "secondary")
    init_secondary(secondary, "secondary-node", "primary-node")

    secondary_scheduler = SerialJobScheduler(secondary)
    secondary_ids = {job["id"] for job in secondary_scheduler.as_dict()["jobs"]}
    assert "secondary_tick" in secondary_ids
    assert "capture_tick" not in secondary_ids
    assert not any(str(job_id).startswith("sync:") for job_id in secondary_ids)

    primary = BrainPaths.from_value(tmp_path / "primary")
    write_sync_config(
        primary,
        SyncConfig(
            node_id="primary-node",
            role="primary",
            brain_home=primary.home,
            primary=PrimaryConfig(
                peers=[
                    PeerConfig(
                        node_id="secondary-a",
                        host="secondary-a.local",
                        user="peter",
                        brain_home=Path("/tmp/secondary-a"),
                    ),
                    PeerConfig(
                        node_id="secondary-b",
                        host="secondary-b.local",
                        user="peter",
                        brain_home=Path("/tmp/secondary-b"),
                    ),
                ]
            ),
        ),
    )

    primary_scheduler = SerialJobScheduler(primary)
    primary_ids = {job["id"] for job in primary_scheduler.as_dict()["jobs"]}
    assert "capture_tick" in primary_ids
    assert "secondary_tick" not in primary_ids
    assert {"sync:secondary-a", "sync:secondary-b"}.issubset(primary_ids)
