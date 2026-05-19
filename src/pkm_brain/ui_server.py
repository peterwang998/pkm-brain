from __future__ import annotations

import json
import os
import secrets
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .audit import audit_memories
from .automation import index_status
from .paths import BrainPaths
from .scheduler.launchd import LaunchdScheduler
from .service import BrainService
from .setup_wizard import build_setup_plan


LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


class BrainUIServer(ThreadingHTTPServer):
    paths: BrainPaths
    token: str


class BrainUIHandler(BaseHTTPRequestHandler):
    server: BrainUIServer

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if not parsed.path.startswith("/api/"):
            self.write_html(ui_shell())
            return
        if not self.authorized():
            self.write_json({"error": "missing or invalid bearer token"}, status=HTTPStatus.UNAUTHORIZED)
            return
        self.dispatch_get(parsed.path, parse_qs(parsed.query))

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if not parsed.path.startswith("/api/"):
            self.write_json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)
            return
        if not self.authorized():
            self.write_json({"error": "missing or invalid bearer token"}, status=HTTPStatus.UNAUTHORIZED)
            return
        self.dispatch_post(parsed.path)

    def log_message(self, format: str, *args: Any) -> None:
        return

    def authorized(self) -> bool:
        header = self.headers.get("Authorization", "")
        return header == f"Bearer {self.server.token}"

    def dispatch_get(self, path: str, query: dict[str, list[str]]) -> None:
        try:
            if path == "/api/status":
                self.write_json(ui_status(self.server.paths))
            elif path == "/api/setup":
                self.write_json(build_setup_plan(self.server.paths))
            elif path == "/api/sync/status":
                self.write_json(service(self.server.paths).sync_status())
            elif path == "/api/sync/conflicts":
                self.write_json(service(self.server.paths).sync_conflicts())
            elif path == "/api/jobs/status":
                self.write_json(ui_jobs_status())
            elif path == "/api/logs":
                self.write_json(ui_logs(self.server.paths))
            elif path == "/api/memory":
                self.write_json(ui_memories(self.server.paths, query))
            elif path.startswith("/api/memory/"):
                memory_id = path.removeprefix("/api/memory/").strip("/")
                self.write_json(service(self.server.paths).get_memory(memory_id))
            else:
                self.write_json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)
        except ValueError as exc:
            self.write_json({"error": str(exc)}, status=HTTPStatus.NOT_FOUND)
        except Exception as exc:
            self.write_json({"error": str(exc)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)

    def dispatch_post(self, path: str) -> None:
        try:
            parts = [part for part in path.removeprefix("/api/").split("/") if part]
            if len(parts) != 3 or parts[0] != "memory":
                self.write_json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)
                return
            _, memory_id, action = parts
            svc = service(self.server.paths)
            if action == "approve":
                self.write_json(svc.approve_memory(memory_id))
            elif action == "reject":
                payload = self.read_json_body()
                self.write_json(svc.reject_memory(memory_id, str(payload.get("reason") or "")))
            elif action == "archive":
                self.write_json(svc.archive_memory(memory_id))
            else:
                self.write_json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)
        except ValueError as exc:
            self.write_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            self.write_json({"error": str(exc)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)

    def read_json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        raw = self.rfile.read(length).decode("utf-8")
        return json.loads(raw)

    def write_html(self, body: str, status: HTTPStatus = HTTPStatus.OK) -> None:
        encoded = body.encode("utf-8")
        self.send_response(status.value)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def write_json(self, payload: dict[str, Any] | list[Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        encoded = json.dumps(payload, sort_keys=True).encode("utf-8")
        self.send_response(status.value)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


def create_ui_server(paths: BrainPaths, host: str, port: int, token: str | None = None) -> BrainUIServer:
    token = token or ensure_ui_token(paths)
    server = BrainUIServer((host, port), BrainUIHandler)
    server.paths = paths
    server.token = token
    return server


def ensure_ui_token(paths: BrainPaths) -> str:
    token_path = ui_token_path(paths)
    token_path.parent.mkdir(parents=True, exist_ok=True)
    if token_path.exists():
        token = token_path.read_text(encoding="utf-8").strip()
        os.chmod(token_path, 0o600)
        if token:
            return token
    token = secrets.token_urlsafe(32)
    token_path.write_text(f"{token}\n", encoding="utf-8")
    os.chmod(token_path, 0o600)
    return token


def ui_token_path(paths: BrainPaths) -> Path:
    return paths.config_local / "ui_token"


def validate_ui_bind(host: str, *, allow_lan: bool = False) -> dict[str, Any]:
    warning = None
    if host not in LOOPBACK_HOSTS:
        warning = f"warning: binding brain ui to {host} may expose the control plane on your LAN"
        if not allow_lan:
            raise ValueError(f"{warning}; pass --i-understand-this-binds-to-lan to continue")
    return {"host": host, "default_host": "127.0.0.1", "allowed": True, "warning": warning}


def ui_status(paths: BrainPaths) -> dict[str, Any]:
    svc = service(paths)
    svc.init_workspace()
    return {
        "doctor": svc.doctor(),
        "sync": safe_call(svc.sync_status),
        "index": index_status(paths, svc),
        "memory": audit_memories(paths),
    }


def ui_memories(paths: BrainPaths, query: dict[str, list[str]]) -> dict[str, Any]:
    svc = service(paths)
    memories = svc.list_memories(
        status=first(query, "status"),
        scope=first(query, "scope"),
        memory_type=first(query, "memory_type"),
        project=first(query, "project"),
    )
    return {"memories": memories, "count": len(memories)}


def ui_jobs_status() -> dict[str, Any]:
    try:
        return {"jobs": [status.as_dict() for status in LaunchdScheduler().status()]}
    except Exception as exc:
        return {"jobs": [], "error": str(exc)}


def ui_logs(paths: BrainPaths) -> dict[str, Any]:
    logs: list[dict[str, Any]] = []
    if paths.logs.exists():
        for path in sorted(paths.logs.glob("*.log")):
            logs.append({"name": path.name, "path": str(path), "bytes": path.stat().st_size})
    return {"logs": logs}


def service(paths: BrainPaths) -> BrainService:
    return BrainService(paths, prefer_model_embeddings=False)


def safe_call(fn: Any) -> dict[str, Any]:
    try:
        return fn()
    except Exception as exc:
        return {"error": str(exc)}


def first(query: dict[str, list[str]], key: str) -> str | None:
    values = query.get(key) or []
    value = values[0].strip() if values else ""
    return value or None


def ui_shell() -> str:
    return """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>PKM Brain</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <style>
    body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; margin: 2rem; color: #1f2933; }
    nav { display: flex; flex-wrap: wrap; gap: .75rem; margin: 1rem 0 2rem; }
    a { color: #0f766e; }
    code { background: #eef2f7; padding: .15rem .3rem; border-radius: 4px; }
  </style>
</head>
<body>
  <h1>PKM Brain</h1>
  <nav>
    <a href="/api/status">Status</a>
    <a href="/api/setup">Setup</a>
    <a href="/api/sync/status">Sync</a>
    <a href="/api/jobs/status">Jobs</a>
    <a href="/api/logs">Logs</a>
    <a href="/api/memory">Memory Review</a>
  </nav>
  <p>API requests require <code>Authorization: Bearer &lt;token&gt;</code>.</p>
</body>
</html>
"""
