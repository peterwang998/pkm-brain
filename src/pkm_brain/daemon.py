from __future__ import annotations

import fcntl
import json
import os
import queue
import secrets
import stat
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from importlib import metadata
from pathlib import Path
from typing import Any, Callable

import yaml

from .automation import (
    as_jsonable,
    run_agent_log_ingest,
    run_gmail_knowledge_ingest,
    run_nightly_maintenance,
    run_secondary_tick,
)
from .gmail_archive_sync import run_scheduled_gmail_archive_sync
from .gmail_sync import run_scheduled_gmail_mirror_sync
from .migrations import MIGRATIONS
from .operational_meeting_packets import run_scheduled_meeting_preparation
from .operational_service import OperationalService, OperationalWriteRefusedError
from .operational_today import OperationalTodayPresentationService
from .paths import BrainPaths
from .shadow_controller import ShadowTrialController
from .service import BrainService
from .sync_config import load_sync_config
from .sync_transfer import sync_run
from .ui_server import BrainUIServer, create_ui_server
from .util import now_iso


DEFAULT_SCHEDULER_TICK_SECONDS = 30.0
DEFAULT_CAPTURE_CADENCE_SECONDS = 600
DEFAULT_NIGHTLY_CHECK_CADENCE_SECONDS = 3600
DEFAULT_SYNC_CADENCE_SECONDS = 1800
DEFAULT_MEETING_PREPARATION_CADENCE_SECONDS = 900
DEFAULT_GMAIL_MIRROR_SYNC_CADENCE_SECONDS = 600
DEFAULT_GMAIL_ARCHIVE_SYNC_CADENCE_SECONDS = 600
DEFAULT_GMAIL_KNOWLEDGE_INGEST_CADENCE_SECONDS = 600


def package_version() -> str:
    try:
        return metadata.version("pkm-brain")
    except metadata.PackageNotFoundError:
        return "0.0.0+local"


def current_schema_version() -> int:
    return max((version for version, _name, _fn in MIGRATIONS), default=0)


def daemon_handshake_path(paths: BrainPaths) -> Path:
    return paths.config_local / "daemon.json"


def daemon_lock_path(paths: BrainPaths) -> Path:
    return paths.config_local / "daemon.lock"


def scheduler_config_path(paths: BrainPaths) -> Path:
    return paths.config_local / "scheduler.yaml"


def atomic_write_private_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    os.chmod(path, 0o600)


def read_daemon_handshake(paths: BrainPaths) -> dict[str, Any] | None:
    path = daemon_handshake_path(paths)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    pid = payload.get("pid")
    if isinstance(pid, int) and pid > 0 and not process_alive(pid):
        return None
    return payload if isinstance(payload, dict) else None


def process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@dataclass
class SchedulerJob:
    id: str
    cadence_s: int
    handler: Callable[[], Any]
    enabled: bool = True
    last_run_at: str | None = None
    last_status: str | None = None
    last_result: dict[str, Any] | None = None
    last_error: str | None = None
    next_due_at: str | None = None
    running: bool = False
    lane: str = "serial"
    run_on_start: bool = False


@dataclass
class SchedulerConfig:
    paused_until: str | None = None
    job_enabled: dict[str, bool] = field(default_factory=dict)


class SerialJobScheduler:
    def __init__(
        self,
        paths: BrainPaths,
        *,
        tick_seconds: float = DEFAULT_SCHEDULER_TICK_SECONDS,
        now: Callable[[], datetime] | None = None,
        jobs: list[SchedulerJob] | None = None,
    ) -> None:
        self.paths = paths
        self.tick_seconds = tick_seconds
        self._now = now or (lambda: datetime.now(timezone.utc).replace(microsecond=0))
        self._lock = threading.RLock()
        self._queued: set[str] = set()
        self._force_run: set[str] = set()
        self._stop = threading.Event()
        self._worker_threads: dict[str, threading.Thread] = {}
        self._ticker_thread: threading.Thread | None = None
        self._config = self._load_config()
        self.jobs: dict[str, SchedulerJob] = {
            job.id: self._apply_config(job)
            for job in (jobs if jobs is not None else build_role_jobs(paths))
        }
        self._queues: dict[str, queue.Queue[str | None]] = {
            lane: queue.Queue() for lane in {job.lane for job in self.jobs.values()}
        }
        for job in self.jobs.values():
            if job.next_due_at is None:
                due_at = (
                    self._now()
                    if job.run_on_start
                    else self._now() + timedelta(seconds=job.cadence_s)
                )
                job.next_due_at = self._iso(due_at)

    def _load_config(self) -> SchedulerConfig:
        path = scheduler_config_path(self.paths)
        if not path.exists():
            return SchedulerConfig()
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        jobs = data.get("jobs") if isinstance(data, dict) else {}
        job_enabled = {
            str(job_id): bool(job_data.get("enabled", True))
            for job_id, job_data in (jobs or {}).items()
            if isinstance(job_data, dict)
        }
        return SchedulerConfig(
            paused_until=str(data.get("paused_until")) if isinstance(data, dict) and data.get("paused_until") else None,
            job_enabled=job_enabled,
        )

    def _save_config(self) -> None:
        scheduler_config_path(self.paths).parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "paused_until": self._config.paused_until,
            "jobs": {job_id: {"enabled": job.enabled} for job_id, job in sorted(self.jobs.items())},
        }
        scheduler_config_path(self.paths).write_text(
            yaml.safe_dump(payload, sort_keys=True, allow_unicode=False),
            encoding="utf-8",
        )

    def _apply_config(self, job: SchedulerJob) -> SchedulerJob:
        if job.id in self._config.job_enabled:
            job.enabled = self._config.job_enabled[job.id]
        return job

    def start(self) -> None:
        with self._lock:
            if self._ticker_thread and self._ticker_thread.is_alive():
                return
            self._stop.clear()
            self._worker_threads = {
                lane: threading.Thread(
                    target=self._worker_loop,
                    args=(lane,),
                    name=f"brain-daemon-worker-{lane}",
                    daemon=True,
                )
                for lane in self._queues
            }
            self._ticker_thread = threading.Thread(
                target=self._ticker_loop, name="brain-daemon-scheduler", daemon=True
            )
            for worker in self._worker_threads.values():
                worker.start()
            self._ticker_thread.start()
            self.enqueue_due_jobs()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        for lane_queue in self._queues.values():
            lane_queue.put(None)
        if self._ticker_thread:
            self._ticker_thread.join(timeout=timeout)
        for worker in self._worker_threads.values():
            worker.join(timeout=timeout)

    def _ticker_loop(self) -> None:
        while not self._stop.wait(self.tick_seconds):
            self.enqueue_due_jobs()

    def _worker_loop(self, lane: str) -> None:
        lane_queue = self._queues[lane]
        while not self._stop.is_set():
            job_id = lane_queue.get()
            if job_id is None:
                return
            with self._lock:
                self._queued.discard(job_id)
                force = job_id in self._force_run
                self._force_run.discard(job_id)
                job = self.jobs.get(job_id)
                if (
                    job is None
                    or job.lane != lane
                    or not job.enabled
                    or (self._is_paused() and not force)
                ):
                    continue
                job.running = True
            started = self._now()
            try:
                raw_result = job.handler()
                result = json.loads(json.dumps(raw_result, default=str))
                status = str(result.get("status") or ("skipped" if result.get("skipped") else "success"))
                error = result.get("error")
                if not error and status == "skipped":
                    error = result.get("reason")
            except Exception as exc:
                result = {}
                status = "failed"
                error = str(exc)
            finished = self._now()
            with self._lock:
                job.running = False
                job.last_run_at = self._iso(started)
                job.last_status = status
                job.last_result = result
                job.last_error = str(error) if error else None
                job.next_due_at = self._iso(finished + timedelta(seconds=job.cadence_s))

    def enqueue_due_jobs(self) -> list[str]:
        if self._is_paused():
            return []
        now = self._now()
        enqueued: list[str] = []
        with self._lock:
            for job in self.jobs.values():
                if not job.enabled or job.running or job.id in self._queued:
                    continue
                due_at = self._parse_iso(job.next_due_at) if job.next_due_at else now
                if due_at <= now:
                    self._enqueue_locked(job.id)
                    enqueued.append(job.id)
        return enqueued

    def run_now(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            if job_id not in self.jobs:
                raise ValueError(f"unknown scheduler job: {job_id}")
            self._enqueue_locked(job_id, force=True)
        return self.as_dict()

    def pause(self, seconds: int) -> dict[str, Any]:
        if seconds <= 0:
            raise ValueError("pause seconds must be positive")
        with self._lock:
            self._config.paused_until = self._iso(self._now() + timedelta(seconds=seconds))
            self._save_config()
        return self.as_dict()

    def resume(self) -> dict[str, Any]:
        with self._lock:
            self._config.paused_until = None
            self._save_config()
        return self.as_dict()

    def set_enabled(self, job_id: str, enabled: bool) -> dict[str, Any]:
        with self._lock:
            if job_id not in self.jobs:
                raise ValueError(f"unknown scheduler job: {job_id}")
            self.jobs[job_id].enabled = enabled
            self._config.job_enabled[job_id] = enabled
            self._save_config()
        return self.as_dict()

    def as_dict(self) -> dict[str, Any]:
        with self._lock:
            return {
                "paused_until": self._config.paused_until,
                "jobs": [
                    {
                        "id": job.id,
                        "enabled": job.enabled,
                        "cadence_s": job.cadence_s,
                        "lane": job.lane,
                        "run_on_start": job.run_on_start,
                        "last_run_at": job.last_run_at,
                        "last_status": job.last_status,
                        "last_result": job.last_result,
                        "last_error": job.last_error,
                        "next_due_at": job.next_due_at,
                        "running": job.running,
                        "queued": job.id in self._queued,
                    }
                    for job in sorted(self.jobs.values(), key=lambda item: item.id)
                ],
            }

    def _enqueue_locked(self, job_id: str, *, force: bool = False) -> None:
        if job_id in self._queued:
            if force:
                self._force_run.add(job_id)
            return
        job = self.jobs.get(job_id)
        if job is None:
            raise ValueError(f"unknown scheduler job: {job_id}")
        self._queued.add(job_id)
        if force:
            self._force_run.add(job_id)
        self._queues[job.lane].put(job_id)

    def _is_paused(self) -> bool:
        paused_until = self._config.paused_until
        if not paused_until:
            return False
        try:
            until = self._parse_iso(paused_until)
        except ValueError:
            return False
        if until <= self._now():
            self._config.paused_until = None
            return False
        return True

    def _iso(self, value: datetime) -> str:
        return value.astimezone(timezone.utc).replace(microsecond=0).isoformat()

    def _parse_iso(self, value: str) -> datetime:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed


def build_role_jobs(
    paths: BrainPaths,
    *,
    operational_service: OperationalService | None = None,
) -> list[SchedulerJob]:
    try:
        sync_config = load_sync_config(paths)
        role = sync_config.role
    except FileNotFoundError:
        sync_config = None
        role = "single"
    jobs: list[SchedulerJob] = []
    if role in {"single", "primary"}:
        jobs.append(
            SchedulerJob(
                id="capture_tick",
                cadence_s=DEFAULT_CAPTURE_CADENCE_SECONDS,
                handler=lambda: as_jsonable(run_agent_log_ingest(paths)),
            )
        )
    if role == "secondary":
        jobs.append(
            SchedulerJob(
                id="secondary_tick",
                cadence_s=DEFAULT_CAPTURE_CADENCE_SECONDS,
                handler=lambda: as_jsonable(run_secondary_tick(paths)),
            )
        )
    jobs.append(
        SchedulerJob(
            id="nightly",
            cadence_s=DEFAULT_NIGHTLY_CHECK_CADENCE_SECONDS,
            handler=lambda: as_jsonable(
                run_nightly_maintenance(paths, if_due=True, due_after_hours=20)
            ),
        )
    )
    if role in {"single", "primary"} and operational_service is not None:
        jobs.append(
            SchedulerJob(
                id="gmail_mirror_sync",
                cadence_s=DEFAULT_GMAIL_MIRROR_SYNC_CADENCE_SECONDS,
                handler=lambda: run_scheduled_gmail_mirror_sync(
                    paths,
                    operational_service,
                ),
                lane="provider_sync",
                run_on_start=True,
            )
        )
        jobs.append(
            SchedulerJob(
                id="gmail_archive_sync",
                cadence_s=DEFAULT_GMAIL_ARCHIVE_SYNC_CADENCE_SECONDS,
                handler=lambda: run_scheduled_gmail_archive_sync(
                    paths,
                    operational_service,
                ),
                lane="provider_sync",
                run_on_start=True,
            )
        )
        jobs.append(
            SchedulerJob(
                id="gmail_knowledge_ingest",
                cadence_s=DEFAULT_GMAIL_KNOWLEDGE_INGEST_CADENCE_SECONDS,
                handler=lambda: run_gmail_knowledge_ingest(paths),
                lane="knowledge_ingest",
            )
        )
        jobs.append(
            SchedulerJob(
                id="meeting_preparation",
                cadence_s=DEFAULT_MEETING_PREPARATION_CADENCE_SECONDS,
                handler=lambda: run_scheduled_meeting_preparation(
                    paths,
                    operational_service,
                ),
            )
        )
    if role == "primary" and sync_config and sync_config.primary:
        for peer in sync_config.primary.peers:
            jobs.append(
                SchedulerJob(
                    id=f"sync:{peer.node_id}",
                    cadence_s=peer.cadence_s or DEFAULT_SYNC_CADENCE_SECONDS,
                    handler=lambda peer_id=peer.node_id: json.loads(
                        json.dumps(sync_run(paths, peer_id, if_reachable=True).__dict__, default=str)
                    ),
                )
            )
    return jobs


@dataclass
class BrainDaemon:
    paths: BrainPaths
    port: int = 0
    serve_web: bool = False
    parent_pid: int | None = None
    scheduler_tick_seconds: float = DEFAULT_SCHEDULER_TICK_SECONDS
    start_scheduler: bool = True
    host: str = "127.0.0.1"
    version: str = field(default_factory=package_version)
    runtime_id: str | None = field(
        default_factory=lambda: os.environ.get("PKM_BRAIN_RUNTIME_ID") or None
    )

    def __post_init__(self) -> None:
        self.token = secrets.token_urlsafe(32)
        self.started_at = now_iso()
        self.server: BrainUIServer | None = None
        self.scheduler: SerialJobScheduler | None = None
        self._lock_file: Any | None = None
        self._lock_owner_pid: int | None = None
        self.operational_service = OperationalService(
            self.paths,
            writer_guard=self._assert_writer_lease,
        )
        self._parent_monitor_stop = threading.Event()
        self._parent_monitor_thread: threading.Thread | None = None

    def start(self) -> None:
        if (
            self.paths.restore_quarantine_file.exists()
            or self.paths.restore_quarantine_file.is_symlink()
        ):
            raise RuntimeError(
                "restored Brain home is quarantined until explicit activation"
            )
        self._acquire_lock()
        try:
            BrainService(self.paths).init_workspace()
            if self.paths.ops_sqlite_path.exists():
                # Existing Chief-of-Staff homes must be migrated before Today or
                # the proactive meeting-preparation job opens the strict store.
                self.operational_service.initialize()
            self.scheduler = SerialJobScheduler(
                self.paths,
                tick_seconds=self.scheduler_tick_seconds,
                jobs=build_role_jobs(
                    self.paths,
                    operational_service=self.operational_service,
                ),
            )
            self.server = create_ui_server(
                self.paths,
                self.host,
                self.port,
                token=self.token,
                serve_static=self.serve_web,
            )
            self.server.daemon_version = self.version
            self.server.daemon_runtime_id = self.runtime_id
            self.server.daemon_started_at = self.started_at
            self.server.daemon_scheduler = self.scheduler
            self.server.daemon_operational_service = self.operational_service
            self.server.daemon_today_service = OperationalTodayPresentationService(
                self.paths,
                self.operational_service,
                scheduler_state_reader=(
                    lambda: self.scheduler.as_dict()
                    if self.scheduler is not None
                    else None
                ),
            )
            self.server.daemon_shadow_controller = ShadowTrialController(
                self.paths,
                self.operational_service,
            )
            self.server.daemon_shutdown_enabled = True
            host, actual_port = self.server.server_address
            payload = {
                "pid": os.getpid(),
                "port": int(actual_port),
                "token": self.token,
                "version": self.version,
                "runtime_id": self.runtime_id,
                "home": str(self.paths.home),
                "started_at": self.started_at,
                "host": str(host),
            }
            atomic_write_private_json(daemon_handshake_path(self.paths), payload)
            self._log(
                {
                    "event": "daemon_started",
                    "port": int(actual_port),
                    "serve_web": self.serve_web,
                }
            )
            self._start_parent_monitor()
            if self.start_scheduler:
                self.scheduler.start()
        except Exception:
            self._parent_monitor_stop.set()
            if self.scheduler is not None:
                self.scheduler.stop()
            if self.server is not None:
                self.server.server_close()
                self.server = None
            self._remove_handshake()
            self._release_lock()
            raise

    def serve_forever(self) -> None:
        self.start()
        assert self.server is not None
        try:
            self.server.serve_forever()
        finally:
            self.close()

    def close(self) -> None:
        self._parent_monitor_stop.set()
        if self._parent_monitor_thread and self._parent_monitor_thread is not threading.current_thread():
            self._parent_monitor_thread.join(timeout=1)
        if self.scheduler:
            self.scheduler.stop()
        if self.server:
            self.server.server_close()
        with self.operational_service.quiesce_mutations():
            self._remove_handshake()
            self._release_lock()
        self._log({"event": "daemon_stopped"})

    def _assert_writer_lease(self) -> None:
        if (
            self._lock_file is None
            or self._lock_file.closed
            or self._lock_owner_pid != os.getpid()
            or not self._daemon_lock_path_matches_descriptor()
        ):
            raise OperationalWriteRefusedError(
                "brain daemon does not hold the writer lease"
            )

    def _acquire_lock(self) -> None:
        lock_path = daemon_lock_path(self.paths)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        if lock_path.is_symlink():
            raise RuntimeError(f"brain daemon lock must not be a symlink: {lock_path}")
        flags = os.O_RDWR | os.O_CREAT
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(lock_path, flags, 0o600)
        try:
            self._lock_file = os.fdopen(descriptor, "a+", encoding="utf-8")
        except Exception:
            os.close(descriptor)
            raise
        descriptor_stat = os.fstat(self._lock_file.fileno())
        if not stat.S_ISREG(descriptor_stat.st_mode):
            self._lock_file.close()
            self._lock_file = None
            raise RuntimeError(f"brain daemon lock is not a regular file: {lock_path}")
        os.fchmod(self._lock_file.fileno(), 0o600)
        try:
            fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self._lock_file.close()
            self._lock_file = None
            self._lock_owner_pid = None
            raise RuntimeError(f"brain daemon is already running for {self.paths.home}") from exc
        if not self._daemon_lock_path_matches_descriptor():
            fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_UN)
            self._lock_file.close()
            self._lock_file = None
            self._lock_owner_pid = None
            raise RuntimeError("brain daemon lock path changed while acquiring the lease")
        self._lock_owner_pid = os.getpid()

    def _daemon_lock_path_matches_descriptor(self) -> bool:
        if self._lock_file is None or self._lock_file.closed:
            return False
        try:
            descriptor_stat = os.fstat(self._lock_file.fileno())
            path_stat = os.stat(
                daemon_lock_path(self.paths),
                follow_symlinks=False,
            )
        except OSError:
            return False
        return (
            stat.S_ISREG(descriptor_stat.st_mode)
            and stat.S_ISREG(path_stat.st_mode)
            and descriptor_stat.st_dev == path_stat.st_dev
            and descriptor_stat.st_ino == path_stat.st_ino
        )

    def _release_lock(self) -> None:
        if self._lock_file is None:
            return
        try:
            fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_UN)
        finally:
            self._lock_file.close()
            self._lock_file = None
            self._lock_owner_pid = None

    def _remove_handshake(self) -> None:
        path = daemon_handshake_path(self.paths)
        if not path.exists():
            return
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            payload = {}
        if payload.get("pid") in {None, os.getpid()}:
            path.unlink(missing_ok=True)

    def _start_parent_monitor(self) -> None:
        if not self.parent_pid or self.parent_pid <= 1:
            return
        self._parent_monitor_thread = threading.Thread(
            target=self._parent_monitor_loop,
            name="brain-daemon-parent-monitor",
            daemon=True,
        )
        self._parent_monitor_thread.start()

    def _parent_monitor_loop(self) -> None:
        assert self.parent_pid is not None
        while not self._parent_monitor_stop.wait(1.0):
            if parent_process_missing(self.parent_pid):
                self._log({"event": "daemon_parent_missing", "parent_pid": self.parent_pid})
                if self.server:
                    self.server.shutdown()
                return

    def _log(self, payload: dict[str, Any]) -> None:
        self.paths.logs.mkdir(parents=True, exist_ok=True)
        row = {"at": now_iso(), **payload}
        with (self.paths.logs / "daemon.log").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, sort_keys=True) + "\n")


def parent_process_missing(parent_pid: int) -> bool:
    return os.getppid() != parent_pid or not process_alive(parent_pid)
