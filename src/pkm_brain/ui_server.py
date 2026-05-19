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
    :root {
      color-scheme: light;
      --bg: #f8fafc;
      --panel: #ffffff;
      --text: #17202a;
      --muted: #5f6c7b;
      --line: #d8e0ea;
      --accent: #0f766e;
      --danger: #b42318;
      --ok: #147d4f;
      --warn: #9a5b00;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      font-size: 14px;
      line-height: 1.45;
    }
    header {
      display: grid;
      grid-template-columns: 1fr minmax(280px, 520px);
      gap: 1rem;
      align-items: end;
      padding: 1rem 1.25rem;
      border-bottom: 1px solid var(--line);
      background: var(--panel);
    }
    h1 { margin: 0; font-size: 1.35rem; font-weight: 700; }
    h2 { margin: 0 0 .75rem; font-size: 1.05rem; }
    .auth {
      display: grid;
      grid-template-columns: 1fr auto auto;
      gap: .5rem;
      align-items: center;
    }
    input, select, button {
      min-height: 2.25rem;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #fff;
      color: var(--text);
      font: inherit;
    }
    input, select { padding: .35rem .55rem; }
    button {
      padding: .35rem .7rem;
      cursor: pointer;
      white-space: nowrap;
    }
    button.primary {
      background: var(--accent);
      border-color: var(--accent);
      color: #fff;
    }
    nav {
      display: flex;
      flex-wrap: wrap;
      gap: .5rem;
      padding: .75rem 1.25rem;
      border-bottom: 1px solid var(--line);
      background: #edf4f7;
    }
    nav button[aria-current="page"] {
      background: #d8f0ec;
      border-color: #98c9c1;
      color: #063f3a;
    }
    main { padding: 1rem 1.25rem 2rem; }
    .toolbar { display: flex; flex-wrap: wrap; gap: .5rem; align-items: center; margin-bottom: .75rem; }
    .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: .75rem; margin-bottom: 1rem; }
    .metric, section {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: .85rem;
    }
    .metric .label { color: var(--muted); font-size: .8rem; }
    .metric .value { font-size: 1.2rem; font-weight: 700; margin-top: .15rem; overflow-wrap: anywhere; }
    table { width: 100%; border-collapse: collapse; background: var(--panel); border: 1px solid var(--line); }
    th, td { padding: .55rem .65rem; border-bottom: 1px solid var(--line); text-align: left; vertical-align: top; }
    th { background: #eef3f7; font-size: .78rem; text-transform: uppercase; color: var(--muted); }
    pre {
      margin: .75rem 0 0;
      padding: .85rem;
      overflow: auto;
      background: #101820;
      color: #e7edf3;
      border-radius: 6px;
      font-size: .8rem;
    }
    .muted { color: var(--muted); }
    .ok { color: var(--ok); }
    .warn { color: var(--warn); }
    .danger { color: var(--danger); }
    .actions { display: flex; flex-wrap: wrap; gap: .35rem; }
    #message { min-height: 1.25rem; color: var(--muted); }
    @media (max-width: 760px) {
      header { grid-template-columns: 1fr; }
      .auth { grid-template-columns: 1fr 1fr; }
      .auth input { grid-column: 1 / -1; }
    }
  </style>
</head>
<body>
  <header>
    <div>
      <h1>PKM Brain</h1>
      <div id="message"></div>
    </div>
    <div class="auth">
      <input id="token-input" type="password" autocomplete="off" placeholder="Token">
      <button id="save-token" class="primary" type="button">Save</button>
      <button id="clear-token" type="button">Clear</button>
    </div>
  </header>
  <nav>
    <button type="button" data-view="status">Status</button>
    <button type="button" data-view="setup">Setup</button>
    <button type="button" data-view="sync">Sync</button>
    <button type="button" data-view="jobs">Jobs</button>
    <button type="button" data-view="logs">Logs</button>
    <button type="button" data-view="memory">Memory Review</button>
  </nav>
  <main id="app" aria-live="polite"></main>
  <script>
    const TOKEN_KEY = "pkm_brain_ui_token";
    const views = {
      status: renderStatus,
      setup: renderSetup,
      sync: renderSync,
      jobs: renderJobs,
      logs: renderLogs,
      memory: renderMemory
    };
    let activeView = "status";
    let memoryStatus = "proposed";

    const app = document.getElementById("app");
    const message = document.getElementById("message");
    const tokenInput = document.getElementById("token-input");
    tokenInput.value = localStorage.getItem(TOKEN_KEY) || "";

    document.getElementById("save-token").addEventListener("click", () => {
      localStorage.setItem(TOKEN_KEY, tokenInput.value.trim());
      setMessage("Token saved.");
      loadView(activeView);
    });
    document.getElementById("clear-token").addEventListener("click", () => {
      localStorage.removeItem(TOKEN_KEY);
      tokenInput.value = "";
      setMessage("Token cleared.");
      loadView(activeView);
    });
    for (const button of document.querySelectorAll("nav button")) {
      button.addEventListener("click", () => loadView(button.dataset.view));
    }

    function token() {
      return tokenInput.value.trim() || localStorage.getItem(TOKEN_KEY) || "";
    }

    async function api(path, options = {}) {
      const currentToken = token();
      if (!currentToken) {
        throw new Error("Token required.");
      }
      const headers = {
        "Authorization": `Bearer ${currentToken}`,
        ...(options.body ? {"Content-Type": "application/json"} : {}),
        ...(options.headers || {})
      };
      const response = await fetch(path, {...options, headers});
      const payload = await response.json();
      if (!response.ok) {
        throw new Error(payload.error || `HTTP ${response.status}`);
      }
      return payload;
    }

    async function loadView(name) {
      activeView = name;
      for (const button of document.querySelectorAll("nav button")) {
        button.setAttribute("aria-current", button.dataset.view === name ? "page" : "false");
      }
      app.innerHTML = `<section><h2>${title(name)}</h2><div class="muted">Loading...</div></section>`;
      setMessage("");
      try {
        await views[name]();
      } catch (error) {
        app.innerHTML = `<section><h2>${title(name)}</h2><div class="danger">${escapeHtml(error.message)}</div></section>`;
      }
    }

    async function renderStatus() {
      const data = await api("/api/status");
      app.innerHTML = `
        <section>
          <div class="toolbar"><h2>Status</h2><button type="button" onclick="loadView('status')">Refresh</button></div>
          <div class="grid">
            ${metric("Home", data.doctor.home)}
            ${metric("SQLite", data.doctor.sqlite ? "ok" : "missing", data.doctor.sqlite ? "ok" : "danger")}
            ${metric("Documents", data.index.documents)}
            ${metric("Chunks", data.index.chunks)}
            ${metric("Memories", data.memory.memories)}
            ${metric("Sync", data.sync.configured === false ? "not configured" : "configured")}
          </div>
          ${jsonBlock(data)}
        </section>`;
    }

    async function renderSetup() {
      const data = await api("/api/setup");
      app.innerHTML = `
        <section>
          <div class="toolbar"><h2>Setup</h2><button type="button" onclick="loadView('setup')">Refresh</button></div>
          <div class="grid">
            ${metric("Role", data.role)}
            ${metric("Node", data.node_id || "")}
            ${metric("Planned Writes", data.planned_writes.length)}
            ${metric("LaunchAgents", data.planned_launch_agent_labels.length)}
          </div>
          ${listSection("Validation Steps", data.validation_steps)}
          ${listSection("Planned Commands", data.planned_commands)}
          ${jsonBlock(data)}
        </section>`;
    }

    async function renderSync() {
      const [status, conflicts] = await Promise.all([api("/api/sync/status"), api("/api/sync/conflicts")]);
      const peers = status.peers || [];
      app.innerHTML = `
        <section>
          <div class="toolbar"><h2>Sync</h2><button type="button" onclick="loadView('sync')">Refresh</button></div>
          <div class="grid">
            ${metric("Configured", status.configured ? "yes" : "no")}
            ${metric("Role", status.role || "")}
            ${metric("Node", status.node_id || "")}
            ${metric("Conflicts", conflicts.count || 0, conflicts.count ? "warn" : "ok")}
          </div>
          ${renderPeerTable(peers)}
          ${listSection("Warnings", status.warnings || [])}
          ${jsonBlock({status, conflicts})}
        </section>`;
    }

    async function renderJobs() {
      const data = await api("/api/jobs/status");
      const jobs = data.jobs || [];
      app.innerHTML = `
        <section>
          <div class="toolbar"><h2>Jobs</h2><button type="button" onclick="loadView('jobs')">Refresh</button></div>
          ${data.error ? `<div class="warn">${escapeHtml(data.error)}</div>` : ""}
          <table><thead><tr><th>Label</th><th>Loaded</th><th>Plist</th><th>Path</th></tr></thead>
          <tbody>${jobs.map(job => `<tr><td>${escapeHtml(job.label)}</td><td>${job.loaded ? "yes" : "no"}</td><td>${job.plist_exists ? "yes" : "no"}</td><td>${escapeHtml(job.path)}</td></tr>`).join("") || emptyRow(4)}</tbody></table>
          ${jsonBlock(data)}
        </section>`;
    }

    async function renderLogs() {
      const data = await api("/api/logs");
      const logs = data.logs || [];
      app.innerHTML = `
        <section>
          <div class="toolbar"><h2>Logs</h2><button type="button" onclick="loadView('logs')">Refresh</button></div>
          <table><thead><tr><th>Name</th><th>Bytes</th><th>Path</th></tr></thead>
          <tbody>${logs.map(log => `<tr><td>${escapeHtml(log.name)}</td><td>${log.bytes}</td><td>${escapeHtml(log.path)}</td></tr>`).join("") || emptyRow(3)}</tbody></table>
        </section>`;
    }

    async function renderMemory() {
      const data = await api(`/api/memory?status=${encodeURIComponent(memoryStatus)}`);
      const memories = data.memories || [];
      app.innerHTML = `
        <section>
          <div class="toolbar">
            <h2>Memory Review</h2>
            <select id="memory-status">
              ${["proposed", "active", "rejected", "archived"].map(value => `<option value="${value}" ${value === memoryStatus ? "selected" : ""}>${value}</option>`).join("")}
            </select>
            <button type="button" onclick="loadView('memory')">Refresh</button>
          </div>
          <table><thead><tr><th>ID</th><th>Type</th><th>Scope</th><th>Content</th><th>Actions</th></tr></thead>
          <tbody>${memories.map(memoryRow).join("") || emptyRow(5)}</tbody></table>
        </section>`;
      document.getElementById("memory-status").addEventListener("change", event => {
        memoryStatus = event.target.value;
        loadView("memory");
      });
    }

    function memoryRow(memory) {
      const actions = memory.status === "proposed"
        ? `<div class="actions"><button type="button" onclick="memoryAction('${memory.id}', 'approve')">Approve</button><button type="button" onclick="rejectMemory('${memory.id}')">Reject</button><button type="button" onclick="memoryAction('${memory.id}', 'archive')">Archive</button></div>`
        : `<button type="button" onclick="memoryAction('${memory.id}', 'archive')">Archive</button>`;
      return `<tr><td>${escapeHtml(memory.id)}</td><td>${escapeHtml(memory.memory_type)}</td><td>${escapeHtml(memory.scope)}</td><td>${escapeHtml(memory.content)}</td><td>${actions}</td></tr>`;
    }

    async function memoryAction(id, action, body) {
      await api(`/api/memory/${encodeURIComponent(id)}/${action}`, {method: "POST", body: body ? JSON.stringify(body) : undefined});
      setMessage(`${action} saved.`);
      await loadView("memory");
    }

    async function rejectMemory(id) {
      const reason = prompt("Reject reason");
      if (reason) {
        await memoryAction(id, "reject", {reason});
      }
    }

    function renderPeerTable(peers) {
      return `<table><thead><tr><th>Peer</th><th>Host</th><th>Mirror</th><th>Pending Outbox</th><th>Last Failure</th></tr></thead>
        <tbody>${peers.map(peer => `<tr><td>${escapeHtml(peer.peer_node_id)}</td><td>${escapeHtml(peer.host || "")}</td><td>${peer.mirror_current ? "current" : "unknown/diverged"}</td><td>${peer.pending_outbox_count ?? ""}</td><td>${escapeHtml(peer.last_failed_run?.error_summary || "")}</td></tr>`).join("") || emptyRow(5)}</tbody></table>`;
    }

    function metric(label, value, state = "") {
      return `<div class="metric"><div class="label">${escapeHtml(label)}</div><div class="value ${state}">${escapeHtml(String(value ?? ""))}</div></div>`;
    }

    function listSection(label, values) {
      if (!values || values.length === 0) return "";
      return `<h2>${escapeHtml(label)}</h2><table><tbody>${values.map(value => `<tr><td>${escapeHtml(String(value))}</td></tr>`).join("")}</tbody></table>`;
    }

    function jsonBlock(data) {
      return `<pre>${escapeHtml(JSON.stringify(data, null, 2))}</pre>`;
    }

    function emptyRow(columns) {
      return `<tr><td colspan="${columns}" class="muted">None</td></tr>`;
    }

    function title(name) {
      return name === "memory" ? "Memory Review" : name.charAt(0).toUpperCase() + name.slice(1);
    }

    function setMessage(text) {
      message.textContent = text;
    }

    function escapeHtml(value) {
      return String(value ?? "").replace(/[&<>"']/g, char => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[char]));
    }

    loadView(activeView);
  </script>
</body>
</html>
"""
