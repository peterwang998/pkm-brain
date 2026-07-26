from __future__ import annotations

import http.client
import json
import os
import signal
import stat
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

import pytest

import pkm_brain.scheduler_worker as scheduler_worker
from pkm_brain.daemon import (
    BrainDaemon,
    SchedulerJob,
    SerialJobScheduler,
    build_role_jobs,
    daemon_handshake_path,
    daemon_lock_path,
    parent_process_missing,
    scheduler_config_path,
)
from pkm_brain.automation import run_nightly_maintenance
from pkm_brain.connectors import load_connector_config, save_connector_config
from pkm_brain.db import connection
from pkm_brain.paths import BrainPaths
from pkm_brain.operational_service import (
    OperationalService,
    OperationalWriteRefusedError,
)
from pkm_brain.scheduler_protocol import (
    SCHEDULER_STATUS_MAX_BYTES,
    read_scheduler_status,
    sanitize_scheduler_payload,
)
from pkm_brain.sync_config import (
    PeerConfig,
    PrimaryConfig,
    SyncConfig,
    write_sync_config,
)
from pkm_brain.sync_setup import init_secondary


@contextmanager
def running_daemon(
    paths: BrainPaths,
    *,
    serve_web: bool = False,
    start_scheduler: bool = False,
) -> Iterator[tuple[BrainDaemon, str, int, str]]:
    runtime = BrainDaemon(
        paths,
        serve_web=serve_web,
        start_scheduler=start_scheduler,
        runtime_id="test-runtime",
    )
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
        assert payload["runtime_id"] == "test-runtime"

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
        assert body["runtime_id"] == "test-runtime"

        status, body = request_json(host, port, "GET", "/api/version", token=token)
        assert status == 200
        assert body["version"]
        assert body["runtime_id"] == "test-runtime"

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


def test_daemon_writer_lease_fences_operational_service(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    runtime = BrainDaemon(paths, start_scheduler=False)

    with pytest.raises(OperationalWriteRefusedError, match="writer lease"):
        runtime.operational_service.initialize()

    runtime.start()
    try:
        runtime.operational_service.initialize()
        assert paths.ops_sqlite_path.exists()
    finally:
        runtime.close()

    with pytest.raises(OperationalWriteRefusedError, match="writer lease"):
        runtime.operational_service.save_source_cursor(
            "calendar",
            "account-1",
            "primary",
            source_type="google_calendar",
            cursor="late-write",
        )


def test_daemon_close_waits_for_operational_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    runtime = BrainDaemon(paths, start_scheduler=False)
    runtime._acquire_lock()
    runtime.operational_service.initialize()
    mutation_entered = threading.Event()
    release_mutation = threading.Event()
    errors: list[BaseException] = []

    def blocking_save(*_args: object, **_kwargs: object) -> dict[str, bool]:
        mutation_entered.set()
        assert release_mutation.wait(timeout=5)
        return {"ok": True}

    monkeypatch.setattr(
        "pkm_brain.operational_service.save_source_cursor",
        blocking_save,
    )

    def mutate() -> None:
        try:
            runtime.operational_service.save_source_cursor(
                "calendar",
                "account-1",
                "primary",
                source_type="google_calendar",
                cursor="cursor-1",
            )
        except BaseException as exc:  # pragma: no cover - assertion reports it
            errors.append(exc)

    mutation = threading.Thread(target=mutate)
    mutation.start()
    assert mutation_entered.wait(timeout=2)
    closing = threading.Thread(target=runtime.close)
    closing.start()

    time.sleep(0.1)
    assert closing.is_alive()
    replacement = BrainDaemon(paths, start_scheduler=False)
    with pytest.raises(RuntimeError, match="already running"):
        replacement._acquire_lock()

    release_mutation.set()
    mutation.join(timeout=2)
    closing.join(timeout=2)
    assert not errors
    assert not mutation.is_alive()
    assert not closing.is_alive()

    replacement._acquire_lock()
    replacement._release_lock()


def test_replaced_daemon_lock_revokes_operational_writer(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    runtime = BrainDaemon(paths, start_scheduler=False)
    runtime._acquire_lock()
    lock_path = daemon_lock_path(paths)
    lock_path.unlink()
    lock_path.touch(mode=0o600)
    replacement = BrainDaemon(paths, start_scheduler=False)
    replacement._acquire_lock()
    try:
        with pytest.raises(OperationalWriteRefusedError, match="writer lease"):
            runtime.operational_service.initialize()
    finally:
        replacement._release_lock()
        runtime._release_lock()


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


def test_scheduler_runs_distinct_lanes_concurrently_on_start(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    provider_started = threading.Event()
    maintenance_started = threading.Event()
    release = threading.Event()
    active = 0
    max_active = 0
    lock = threading.Lock()

    def handler(started: threading.Event) -> dict[str, str]:
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
        started.set()
        assert release.wait(timeout=3)
        with lock:
            active -= 1
        return {"status": "success"}

    scheduler = SerialJobScheduler(
        paths,
        tick_seconds=60,
        jobs=[
            SchedulerJob(
                "maintenance",
                3600,
                lambda: handler(maintenance_started),
                run_on_start=True,
            ),
            SchedulerJob(
                "provider",
                600,
                lambda: handler(provider_started),
                lane="provider_sync",
                run_on_start=True,
            ),
        ],
    )
    scheduler.start()
    try:
        assert maintenance_started.wait(timeout=1)
        assert provider_started.wait(timeout=1)
        assert max_active == 2
    finally:
        release.set()
        scheduler.stop()

    jobs = {job["id"]: job for job in scheduler.as_dict()["jobs"]}
    assert jobs["maintenance"]["lane"] == "serial"
    assert jobs["provider"]["lane"] == "provider_sync"


def test_scheduler_runs_isolated_job_in_child_instead_of_handler(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    handler_called = False

    def handler() -> dict[str, str]:
        nonlocal handler_called
        handler_called = True
        return {"status": "failed"}

    scheduler = SerialJobScheduler(
        paths,
        tick_seconds=60,
        jobs=[
            SchedulerJob(
                "capture_tick",
                60,
                handler,
                lane="knowledge_mutation",
                isolated_job="capture_tick",
            )
        ],
    )

    def command(_job: str, status_path: Path) -> list[str]:
        source = (
            "from pathlib import Path; "
            f"Path({str(status_path)!r}).write_text("
            '\'{"status":"success","processed":3}\', encoding=\'utf-8\')'
        )
        return [sys.executable, "-c", source]

    monkeypatch.setattr(scheduler, "_isolated_command", command)
    scheduler.run_now("capture_tick")
    scheduler.start()
    try:
        deadline = time.time() + 3
        while time.time() < deadline:
            state = scheduler.as_dict()["jobs"][0]
            if state["last_status"]:
                break
            time.sleep(0.01)
    finally:
        scheduler.stop()

    state = scheduler.as_dict()["jobs"][0]
    assert handler_called is False
    assert state["isolated"] is True
    assert state["last_status"] == "success"
    assert state["last_result"]["processed"] == 3


def test_scheduler_guardian_command_is_bound_to_daemon_pid(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    scheduler = SerialJobScheduler(
        paths,
        jobs=[
            SchedulerJob(
                "capture_tick",
                60,
                lane="knowledge_mutation",
                isolated_job="capture_tick",
            )
        ],
    )
    command = scheduler._isolated_command(
        "capture_tick",
        paths.logs / "status.json",
    )

    parent_index = command.index("--parent-pid")
    assert command[parent_index + 1] == str(os.getpid())


def test_scheduler_guardian_reports_executor_signal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    status_path = tmp_path / "status.json"
    monkeypatch.setattr(
        scheduler_worker,
        "supervise_scheduled_process",
        lambda *_args, **_kwargs: -signal.SIGBUS,
    )

    exit_code = scheduler_worker.main(
        [
            "--job",
            "capture_tick",
            "--home",
            str(tmp_path / "brain"),
            "--status-file",
            str(status_path),
            "--parent-pid",
            str(os.getpid()),
        ]
    )
    status = read_scheduler_status(status_path)

    assert exit_code == 128 + signal.SIGBUS
    assert status == {
        "status": "failed",
        "error": "scheduled executor terminated by signal",
        "signal_number": signal.SIGBUS,
        "signal_name": "SIGBUS",
    }


def test_scheduler_stop_terminates_active_isolated_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    scheduler = SerialJobScheduler(
        paths,
        tick_seconds=60,
        jobs=[
            SchedulerJob(
                "capture_tick",
                60,
                lambda: {"status": "failed"},
                lane="knowledge_mutation",
                isolated_job="capture_tick",
            )
        ],
    )
    monkeypatch.setattr(
        scheduler,
        "_isolated_command",
        lambda _job, _status_path: [
            sys.executable,
            "-c",
            "import time; time.sleep(60)",
        ],
    )
    scheduler.run_now("capture_tick")
    scheduler.start()
    child = None
    deadline = time.time() + 3
    while child is None and time.time() < deadline:
        with scheduler._children_lock:
            child = scheduler._active_children.get("capture_tick")
        time.sleep(0.01)
    assert child is not None
    assert child.poll() is None

    started = time.monotonic()
    scheduler.stop(timeout=1)
    elapsed = time.monotonic() - started

    assert elapsed < 2
    assert child.poll() is not None
    assert not any(worker.is_alive() for worker in scheduler._worker_threads.values())


def test_scheduler_survives_isolated_child_crash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    scheduler = SerialJobScheduler(
        paths,
        tick_seconds=60,
        jobs=[
            SchedulerJob(
                "capture_tick",
                60,
                lambda: {"status": "failed"},
                lane="knowledge_mutation",
                isolated_job="capture_tick",
            )
        ],
    )
    invocations = 0

    def command(_job: str, status_path: Path) -> list[str]:
        nonlocal invocations
        invocations += 1
        if invocations == 1:
            return [sys.executable, "-c", "import os; os._exit(17)"]
        source = (
            "from pathlib import Path; "
            f"Path({str(status_path)!r}).write_text("
            "'{\"status\":\"success\"}', encoding='utf-8')"
        )
        return [sys.executable, "-c", source]

    monkeypatch.setattr(scheduler, "_isolated_command", command)
    scheduler.run_now("capture_tick")
    scheduler.start()
    try:
        deadline = time.time() + 3
        while time.time() < deadline:
            state = scheduler.as_dict()["jobs"][0]
            if state["last_status"] == "failed":
                break
            time.sleep(0.01)
        first = scheduler.as_dict()["jobs"][0]
        assert first["last_status"] == "failed"
        assert first["last_result"]["exit_code"] == 17

        scheduler.run_now("capture_tick")
        deadline = time.time() + 3
        while time.time() < deadline:
            state = scheduler.as_dict()["jobs"][0]
            if invocations == 2 and state["last_status"] == "success":
                break
            time.sleep(0.01)
    finally:
        scheduler.stop()

    assert scheduler.as_dict()["jobs"][0]["last_status"] == "success"


def test_scheduler_cleans_executor_group_after_guardian_crash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    executor_pid_path = tmp_path / "executor.pid"
    scheduler = SerialJobScheduler(
        paths,
        tick_seconds=60,
        jobs=[
            SchedulerJob(
                "capture_tick",
                60,
                lane="knowledge_mutation",
                isolated_job="capture_tick",
            )
        ],
    )
    executor_source = "import time; time.sleep(60)"
    guardian_source = (
        "import os, subprocess, sys\n"
        "from pathlib import Path\n"
        "executor = subprocess.Popen("
        f"[sys.executable, '-c', {executor_source!r}], "
        "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, "
        "stderr=subprocess.DEVNULL, close_fds=True)\n"
        f"Path({str(executor_pid_path)!r}).write_text("
        "str(executor.pid), encoding='utf-8')\n"
        "os._exit(23)\n"
    )
    monkeypatch.setattr(
        scheduler,
        "_isolated_command",
        lambda _job, _status_path: [sys.executable, "-c", guardian_source],
    )
    scheduler.run_now("capture_tick")
    scheduler.start()
    executor_pid: int | None = None
    try:
        deadline = time.time() + 5
        while time.time() < deadline:
            state = scheduler.as_dict()["jobs"][0]
            if state["last_status"]:
                break
            time.sleep(0.01)
        state = scheduler.as_dict()["jobs"][0]
        assert state["last_status"] == "failed"
        assert state["last_result"]["exit_code"] == 23
        executor_pid = int(executor_pid_path.read_text(encoding="utf-8"))
        assert _wait_for_process_exit(executor_pid, timeout=3)
    finally:
        scheduler.stop()
        if executor_pid is not None:
            try:
                os.kill(executor_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_scheduler_guardian_kills_executor_when_daemon_parent_dies(
    tmp_path: Path,
) -> None:
    guardian_pid_path = tmp_path / "guardian.pid"
    executor_pid_path = tmp_path / "executor.pid"
    forbidden_write = tmp_path / "orphan-write.txt"
    executor_source = (
        "import os, time\n"
        "from pathlib import Path\n"
        f"Path({str(executor_pid_path)!r}).write_text(str(os.getpid()), encoding='utf-8')\n"
        "time.sleep(1.5)\n"
        f"Path({str(forbidden_write)!r}).write_text('orphan write', encoding='utf-8')\n"
    )
    guardian_source = (
        "import os, sys\n"
        "from pkm_brain.scheduler_worker import supervise_scheduled_process\n"
        "raise SystemExit(supervise_scheduled_process("
        "os.getppid(), "
        f"[sys.executable, '-c', {executor_source!r}], poll_interval=0.02))\n"
    )
    parent_source = (
        "import os, signal, subprocess, sys, time\n"
        "from pathlib import Path\n"
        "guardian = subprocess.Popen("
        f"[sys.executable, '-c', {guardian_source!r}], "
        "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, "
        "stderr=subprocess.DEVNULL, close_fds=True, start_new_session=True)\n"
        f"Path({str(guardian_pid_path)!r}).write_text(str(guardian.pid), encoding='utf-8')\n"
        f"ready = Path({str(executor_pid_path)!r})\n"
        "deadline = time.monotonic() + 5\n"
        "while not ready.exists() and time.monotonic() < deadline:\n"
        "    time.sleep(0.01)\n"
        "if not ready.exists():\n"
        "    os.killpg(guardian.pid, signal.SIGKILL)\n"
        "    os._exit(2)\n"
        "os.kill(os.getpid(), signal.SIGKILL)\n"
    )
    parent = subprocess.Popen(
        [sys.executable, "-c", parent_source],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )
    guardian_pid: int | None = None
    executor_pid: int | None = None
    try:
        assert parent.wait(timeout=8) == -signal.SIGKILL
        guardian_pid = int(guardian_pid_path.read_text(encoding="utf-8"))
        executor_pid = int(executor_pid_path.read_text(encoding="utf-8"))

        time.sleep(1.7)

        assert not forbidden_write.exists()
        assert _wait_for_process_exit(guardian_pid, timeout=3)
        assert _wait_for_process_exit(executor_pid, timeout=3)
    finally:
        if guardian_pid is not None:
            try:
                os.killpg(guardian_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_scheduler_status_is_bounded_and_redacts_credentials() -> None:
    sanitized = sanitize_scheduler_payload(
        {
            "status": "success",
            "token": "top-secret",
            "token_count": 7,
            "nested": {
                "provider_api_key": "also-secret",
                "message": "ok",
            },
        }
    )
    assert sanitized["token"] == "[redacted]"
    assert sanitized["token_count"] == 7
    assert sanitized["nested"]["provider_api_key"] == "[redacted]"

    oversized = sanitize_scheduler_payload(
        {
            "status": "success",
            **{f"field_{index}": "x" * 10_000 for index in range(100)},
        }
    )
    assert oversized["status"] == "success"
    assert oversized["truncated"] is True
    assert len(json.dumps(oversized).encode("utf-8")) <= SCHEDULER_STATUS_MAX_BYTES


def test_scheduler_does_not_overlap_repeated_run_for_same_job(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    first_started = threading.Event()
    release_first = threading.Event()
    active = 0
    max_active = 0
    invocation_count = 0
    lock = threading.Lock()

    def handler() -> dict[str, str]:
        nonlocal active, max_active, invocation_count
        with lock:
            active += 1
            max_active = max(max_active, active)
            invocation_count += 1
            invocation = invocation_count
        if invocation == 1:
            first_started.set()
            assert release_first.wait(timeout=3)
        with lock:
            active -= 1
        return {"status": "success"}

    scheduler = SerialJobScheduler(
        paths,
        tick_seconds=60,
        jobs=[SchedulerJob("job", 60, handler)],
    )
    scheduler.run_now("job")
    scheduler.start()
    try:
        assert first_started.wait(timeout=1)
        scheduler.run_now("job")
        release_first.set()
        deadline = time.time() + 3
        while invocation_count < 2 and time.time() < deadline:
            time.sleep(0.01)
    finally:
        release_first.set()
        scheduler.stop()

    assert invocation_count == 2
    assert max_active == 1


def test_scheduler_pause_persists(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    scheduler = SerialJobScheduler(
        paths, jobs=[SchedulerJob("job", 60, lambda: {"status": "success"})]
    )

    state = scheduler.pause(120)
    assert state["paused_until"]
    assert scheduler_config_path(paths).exists()

    reloaded = SerialJobScheduler(
        paths, jobs=[SchedulerJob("job", 60, lambda: {"status": "success"})]
    )
    assert reloaded.as_dict()["paused_until"] == state["paused_until"]

    resumed = reloaded.resume()
    assert resumed["paused_until"] is None


def test_scheduler_preserves_skipped_reason(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    scheduler = SerialJobScheduler(
        paths,
        jobs=[
            SchedulerJob(
                "job",
                60,
                lambda: {
                    "status": "skipped",
                    "reason": "last successful nightly run is less than 20 hours old",
                },
            )
        ],
    )
    scheduler.run_now("job")
    scheduler.start()
    try:
        deadline = time.time() + 3
        while time.time() < deadline:
            job = scheduler.as_dict()["jobs"][0]
            if job["last_status"]:
                break
            time.sleep(0.01)
    finally:
        scheduler.stop()

    job = scheduler.as_dict()["jobs"][0]
    assert job["last_status"] == "skipped"
    assert job["last_error"] == "last successful nightly run is less than 20 hours old"
    assert (
        job["last_result"]["reason"]
        == "last successful nightly run is less than 20 hours old"
    )


def test_scheduler_run_now_bypasses_pause(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    completed: list[str] = []
    scheduler = SerialJobScheduler(
        paths,
        jobs=[
            SchedulerJob(
                "job", 60, lambda: completed.append("job") or {"status": "success"}
            )
        ],
    )
    scheduler.pause(3600)
    scheduler.start()
    try:
        scheduler.run_now("job")
        deadline = time.time() + 3
        while not completed and time.time() < deadline:
            time.sleep(0.01)
    finally:
        scheduler.stop()

    assert completed == ["job"]


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
                        cadence_s=900,
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
    primary_jobs = {str(job["id"]): job for job in primary_scheduler.as_dict()["jobs"]}
    primary_ids = set(primary_jobs)
    assert "capture_tick" in primary_ids
    assert "secondary_tick" not in primary_ids
    assert {"sync:secondary-a", "sync:secondary-b"}.issubset(primary_ids)
    assert primary_jobs["sync:secondary-a"]["cadence_s"] == 900
    assert primary_jobs["sync:secondary-b"]["cadence_s"] == 1800
    assert primary_jobs["sync:secondary-a"]["lane"] == "knowledge_mutation"
    assert primary_jobs["sync:secondary-b"]["lane"] == "knowledge_mutation"
    assert primary_jobs["sync:secondary-a"]["isolated"] is True
    assert primary_jobs["sync:secondary-b"]["isolated"] is True


def test_scheduler_worker_only_dispatches_exact_configured_sync_peer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = BrainPaths.from_value(tmp_path / "primary")
    write_sync_config(
        paths,
        SyncConfig(
            node_id="primary-node",
            role="primary",
            brain_home=paths.home,
            primary=PrimaryConfig(
                peers=[
                    PeerConfig(
                        node_id="secondary-a",
                        host="secondary-a.local",
                        user="peter",
                        brain_home=Path("/tmp/secondary-a"),
                    )
                ]
            ),
        ),
    )
    calls: list[tuple[str, bool]] = []

    class Result:
        def as_dict(self) -> dict[str, str]:
            return {"status": "ok", "peer_node_id": "secondary-a"}

    def sync(
        _paths: BrainPaths,
        peer_node_id: str,
        *,
        if_reachable: bool,
    ) -> Result:
        calls.append((peer_node_id, if_reachable))
        return Result()

    monkeypatch.setattr("pkm_brain.sync_transfer.sync_run", sync)
    result = scheduler_worker.run_scheduled_child("sync:secondary-a", paths)

    assert result == {"status": "ok", "peer_node_id": "secondary-a"}
    assert calls == [("secondary-a", True)]
    with pytest.raises(ValueError, match="unsupported isolated scheduler job"):
        scheduler_worker.run_scheduled_child("syncish:secondary-a", paths)
    with pytest.raises(ValueError, match="configured peer"):
        scheduler_worker.run_scheduled_child("sync:secondary-b", paths)


def test_gmail_jobs_are_primary_lane_startup_work(tmp_path: Path) -> None:
    for role in ("single", "primary"):
        paths = BrainPaths.from_value(tmp_path / role)
        if role == "primary":
            write_sync_config(
                paths,
                SyncConfig(
                    node_id="primary-node",
                    role="primary",
                    brain_home=paths.home,
                    primary=PrimaryConfig(peers=[]),
                ),
            )
        service = OperationalService(paths, writer_guard=lambda: None)
        jobs = {
            job.id: job for job in build_role_jobs(paths, operational_service=service)
        }

        gmail = jobs["gmail_mirror_sync"]
        assert gmail.cadence_s == 600
        assert gmail.lane == "provider_sync"
        assert gmail.run_on_start is True
        archive = jobs["gmail_archive_sync"]
        assert archive.cadence_s == 600
        assert archive.lane == "provider_sync"
        assert archive.run_on_start is True
        assert list(jobs).index("gmail_mirror_sync") < list(jobs).index(
            "gmail_archive_sync"
        )
        knowledge = jobs["gmail_knowledge_ingest"]
        assert knowledge.cadence_s == 600
        assert knowledge.lane == "knowledge_mutation"
        assert knowledge.run_on_start is False
        assert knowledge.isolated_job == "gmail_knowledge_ingest"
        mutation_jobs = {
            job.id: job
            for job in jobs.values()
            if job.id in {"capture_tick", "nightly", "gmail_knowledge_ingest"}
        }
        assert set(mutation_jobs) == {
            "capture_tick",
            "nightly",
            "gmail_knowledge_ingest",
        }
        assert {job.lane for job in mutation_jobs.values()} == {"knowledge_mutation"}
        assert all(job.isolated_job == job.id for job in mutation_jobs.values())

    secondary = BrainPaths.from_value(tmp_path / "secondary-with-ops")
    init_secondary(secondary, "secondary-node", "primary-node")
    service = OperationalService(secondary, writer_guard=lambda: None)
    secondary_ids = {
        job.id for job in build_role_jobs(secondary, operational_service=service)
    }
    assert "gmail_mirror_sync" not in secondary_ids
    assert "gmail_archive_sync" not in secondary_ids
    assert "gmail_knowledge_ingest" not in secondary_ids


def test_daemon_nightly_summary_matches_automation_shape(tmp_path: Path) -> None:
    direct = BrainPaths.from_value(tmp_path / "direct")
    daemon = BrainPaths.from_value(tmp_path / "daemon")
    disable_all_connectors(direct)
    disable_all_connectors(daemon)

    direct_result = run_nightly_maintenance(direct, if_due=True, due_after_hours=20)
    scheduler = SerialJobScheduler(daemon)
    scheduler.start()
    try:
        scheduler.run_now("nightly")
        deadline = time.time() + 8
        while time.time() < deadline:
            nightly = next(
                job for job in scheduler.as_dict()["jobs"] if job["id"] == "nightly"
            )
            if nightly["last_status"]:
                break
            time.sleep(0.05)
    finally:
        scheduler.stop()

    with connection(daemon.sqlite_path) as conn:
        row = conn.execute(
            "SELECT summary FROM automation_runs WHERE job_name = ? ORDER BY started_at DESC LIMIT 1",
            ("nightly-maintenance",),
        ).fetchone()

    assert direct_result.status == "success"
    assert row is not None
    daemon_summary = json.loads(row["summary"])
    assert summary_shape(daemon_summary) == summary_shape(direct_result.summary)


def test_parent_process_missing_checks_ppid_and_liveness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("pkm_brain.daemon.os.getppid", lambda: 123)
    monkeypatch.setattr("pkm_brain.daemon.process_alive", lambda pid: True)
    assert parent_process_missing(123) is False

    monkeypatch.setattr("pkm_brain.daemon.os.getppid", lambda: 1)
    assert parent_process_missing(123) is True

    monkeypatch.setattr("pkm_brain.daemon.os.getppid", lambda: 123)
    monkeypatch.setattr("pkm_brain.daemon.process_alive", lambda pid: False)
    assert parent_process_missing(123) is True


def disable_all_connectors(paths: BrainPaths) -> None:
    config = load_connector_config(paths)
    for state in config["connectors"].values():
        state["enabled"] = False
    save_connector_config(paths, config)


def summary_shape(value: object) -> object:
    if isinstance(value, dict):
        return {key: summary_shape(nested) for key, nested in sorted(value.items())}
    if isinstance(value, list):
        return [summary_shape(value[0])] if value else []
    return type(value).__name__


def _wait_for_process_exit(pid: int, *, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            pass
        time.sleep(0.02)
    return False
