#!/usr/bin/env python3
import json
import os
import signal
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def _arg_value(args, name):
    try:
        return args[args.index(name) + 1]
    except (ValueError, IndexError):
        return None


class Handler(BaseHTTPRequestHandler):
    server_version = "FakeBrain/0.1"

    def log_message(self, format, *args):
        return

    def do_GET(self):
        if not self._authorized():
            self._send(401, {"error": "unauthorized"})
            return
        if self.path == "/api/health":
            self._send(
                200,
                {
                    "ok": True,
                    "version": self.server.version,
                    "home": str(self.server.home),
                    "pid": os.getpid(),
                    "host": "127.0.0.1",
                    "port": self.server.server_port,
                    "started_at": self.server.started_at,
                    "schema_version": 20,
                },
            )
            return
        if self.path == "/api/scheduler":
            self._send(
                200,
                {
                    "paused_until": None,
                    "jobs": [
                        {
                            "id": "capture_tick",
                            "enabled": True,
                            "cadence_s": 600,
                            "last_run_at": None,
                            "last_status": None,
                            "last_error": None,
                            "next_due_at": None,
                            "running": False,
                            "queued": False,
                        }
                    ],
                },
            )
            return
        if self.path == "/api/digest":
            self._send(
                200,
                {
                    "generated_at": self.server.started_at,
                    "since": None,
                    "pulse": [],
                    "latest_run": None,
                    "facts_by_page": [],
                    "reverts": [],
                    "demotions": [],
                    "eval_transitions": [],
                    "queue_counts": {"total": 0, "by_kind": {}, "raw": {}},
                    "raw": {},
                },
            )
            return
        self._send(404, {"error": "not_found"})

    def do_POST(self):
        if not self._authorized():
            self._send(401, {"error": "unauthorized"})
            return
        if self.path == "/api/shutdown":
            self._send(200, {"ok": True})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return
        self._send(200, {"paused_until": None, "jobs": []})

    def _authorized(self):
        return self.headers.get("Authorization") == f"Bearer {self.server.token}"

    def _send(self, status, body):
        payload = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def main():
    args = sys.argv[1:]
    home = _arg_value(args, "--home")
    if args[:1] != ["daemon"] or not home:
        print("fake brain only supports: daemon --home PATH", file=sys.stderr)
        return 2

    home_path = Path(home)
    handshake = home_path / "config" / "local" / "daemon.json"
    handshake.parent.mkdir(parents=True, exist_ok=True)
    token = f"fake-token-{os.getpid()}"
    started_at = "2026-07-08T08:00:00+00:00"

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.home = home_path
    server.token = token
    server.version = os.environ.get("FAKE_BRAIN_VERSION", "0.1.0")
    server.started_at = started_at

    payload = {
        "pid": os.getpid(),
        "port": server.server_port,
        "token": token,
        "version": server.version,
        "home": str(home_path),
        "started_at": started_at,
        "host": "127.0.0.1",
    }
    fd, temp_path = tempfile.mkstemp(dir=handshake.parent, prefix=".daemon.", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)
    os.chmod(temp_path, 0o600)
    os.replace(temp_path, handshake)

    def handle_signal(signum, frame):
        server.shutdown()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    try:
        server.serve_forever()
    finally:
        try:
            handshake.unlink()
        except FileNotFoundError:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
