from __future__ import annotations

import argparse
import os
import select
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, NoReturn

from .paths import BrainPaths
from .scheduler_protocol import write_scheduler_status


PARENT_CHECK_INTERVAL_SECONDS = 0.1
ORPHANED_WORKER_EXIT_CODE = 70


class SchedulerParentExitedError(RuntimeError):
    pass


class ParentLivenessMonitor:
    """Watch the worker's exact parent, using kqueue on macOS when available."""

    def __init__(self, expected_parent_pid: int) -> None:
        if expected_parent_pid <= 1:
            raise ValueError("scheduler parent pid must be greater than one")
        self.expected_parent_pid = expected_parent_pid
        self._kqueue: Any | None = None
        if not self._parent_is_current():
            raise SchedulerParentExitedError("scheduler parent already exited")
        kqueue_factory = getattr(select, "kqueue", None)
        kevent_factory = getattr(select, "kevent", None)
        if kqueue_factory is not None and kevent_factory is not None:
            kqueue = kqueue_factory()
            try:
                event = kevent_factory(
                    expected_parent_pid,
                    filter=select.KQ_FILTER_PROC,
                    flags=select.KQ_EV_ADD | select.KQ_EV_ENABLE,
                    fflags=select.KQ_NOTE_EXIT,
                )
                kqueue.control([event], 0, 0)
            except OSError:
                kqueue.close()
                if not self._parent_is_current():
                    raise SchedulerParentExitedError(
                        "scheduler parent exited during monitor setup"
                    ) from None
            else:
                self._kqueue = kqueue
        if not self._parent_is_current():
            self.close()
            raise SchedulerParentExitedError(
                "scheduler parent exited during monitor setup"
            )

    def parent_exited(self, timeout: float) -> bool:
        if not self._parent_is_current():
            return True
        if self._kqueue is not None:
            try:
                if self._kqueue.control(None, 1, max(0.0, timeout)):
                    return True
            except OSError:
                self.close()
                if not self._parent_is_current():
                    return True
                if timeout > 0:
                    time.sleep(timeout)
        elif timeout > 0:
            time.sleep(timeout)
        return not self._parent_is_current()

    def close(self) -> None:
        if self._kqueue is not None:
            self._kqueue.close()
            self._kqueue = None

    def _parent_is_current(self) -> bool:
        return os.getppid() == self.expected_parent_pid


def supervise_scheduled_process(
    expected_parent_pid: int,
    command: list[str],
    *,
    poll_interval: float = PARENT_CHECK_INTERVAL_SECONDS,
) -> int:
    """Run one executor and kill its process group if the daemon disappears."""

    if os.getpgrp() != os.getpid():
        raise RuntimeError("scheduler guardian requires an isolated process group")
    try:
        monitor = ParentLivenessMonitor(expected_parent_pid)
    except SchedulerParentExitedError:
        _kill_orphaned_process_group()
    executor: subprocess.Popen[bytes] | None = None
    try:
        if monitor.parent_exited(0):
            _kill_orphaned_process_group()
        executor = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
        if monitor.parent_exited(0):
            _kill_orphaned_process_group()
        while True:
            return_code = executor.poll()
            if return_code is not None:
                return return_code
            if monitor.parent_exited(poll_interval):
                _kill_orphaned_process_group()
    except BaseException:
        if executor is not None and executor.poll() is None:
            _kill_orphaned_process_group()
        raise
    finally:
        monitor.close()


def run_scheduled_child(job_id: str, paths: BrainPaths) -> dict[str, Any]:
    # These imports intentionally live only in the heavy executor. The guardian
    # remains stdlib-only so it can observe parent death even while native model
    # or index code is busy, crashed, or holding the executor's GIL.
    from .automation import (
        as_jsonable,
        run_agent_log_ingest,
        run_gmail_knowledge_ingest,
        run_nightly_maintenance,
        run_secondary_tick,
    )

    handlers: dict[str, Callable[[], dict[str, Any]]] = {
        "capture_tick": lambda: as_jsonable(run_agent_log_ingest(paths)),
        "secondary_tick": lambda: as_jsonable(run_secondary_tick(paths)),
        "nightly": lambda: as_jsonable(
            run_nightly_maintenance(paths, if_due=True, due_after_hours=20)
        ),
        "gmail_knowledge_ingest": lambda: run_gmail_knowledge_ingest(paths),
    }
    handler = handlers.get(job_id)
    if handler is not None:
        return handler()
    if job_id.startswith("sync:"):
        return _run_configured_sync(job_id.removeprefix("sync:"), paths)
    raise ValueError(f"unsupported isolated scheduler job: {job_id}")


def _run_configured_sync(peer_node_id: str, paths: BrainPaths) -> dict[str, Any]:
    from .sync_config import load_sync_config
    from .sync_transfer import sync_run

    if not peer_node_id:
        raise ValueError("isolated sync scheduler job is missing its peer")
    config = load_sync_config(paths)
    configured_peer_ids = {
        peer.node_id
        for peer in (config.primary.peers if config.primary is not None else [])
    }
    if config.role != "primary" or peer_node_id not in configured_peer_ids:
        raise ValueError("isolated sync scheduler job does not name a configured peer")
    return sync_run(
        paths,
        peer_node_id,
        if_reachable=True,
    ).as_dict()


def _executor_command(job_id: str, home: str, status_file: str) -> list[str]:
    return [
        sys.executable,
        "-m",
        "pkm_brain.scheduler_worker",
        "--execute",
        "--job",
        job_id,
        "--home",
        home,
        "--status-file",
        status_file,
    ]


def _execute_job(job_id: str, home: str, status_file: str) -> int:
    status_path = Path(status_file)
    try:
        result = run_scheduled_child(
            job_id,
            BrainPaths.from_value(home),
        )
    except BaseException as exc:
        write_scheduler_status(
            status_path,
            {
                "status": "failed",
                "error": "scheduled child failed",
                "error_type": type(exc).__name__,
            },
        )
        return 1
    write_scheduler_status(status_path, result)
    return 0


def _kill_orphaned_process_group() -> NoReturn:
    if os.getpgrp() == os.getpid():
        try:
            os.killpg(os.getpgrp(), signal.SIGKILL)
        except OSError:
            pass
    os._exit(ORPHANED_WORKER_EXIT_CODE)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--job", required=True)
    parser.add_argument("--home", required=True)
    parser.add_argument("--status-file", required=True)
    parser.add_argument("--parent-pid", type=int)
    args = parser.parse_args(argv)
    if args.execute:
        return _execute_job(str(args.job), str(args.home), str(args.status_file))
    if args.parent_pid is None:
        write_scheduler_status(
            Path(args.status_file),
            {
                "status": "failed",
                "error": "scheduled guardian is missing its daemon identity",
            },
        )
        return 1
    try:
        return_code = supervise_scheduled_process(
            int(args.parent_pid),
            _executor_command(
                str(args.job),
                str(args.home),
                str(args.status_file),
            ),
        )
        if return_code < 0:
            signal_number = -return_code
            try:
                signal_name = signal.Signals(signal_number).name
            except ValueError:
                signal_name = "UNKNOWN"
            write_scheduler_status(
                Path(args.status_file),
                {
                    "status": "failed",
                    "error": "scheduled executor terminated by signal",
                    "signal_number": signal_number,
                    "signal_name": signal_name,
                },
            )
            return 128 + signal_number
        return return_code
    except BaseException as exc:
        write_scheduler_status(
            Path(args.status_file),
            {
                "status": "failed",
                "error": "scheduled guardian failed",
                "error_type": type(exc).__name__,
            },
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
