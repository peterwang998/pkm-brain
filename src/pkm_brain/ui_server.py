from __future__ import annotations

import json
import mimetypes
import os
import secrets
import threading
from importlib import resources
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import yaml

from .audit import audit_memories
from .automation import index_status
from .contracts import active_page_contracts, generate_initial_contracts
from .connectors import (
    get_connector,
    list_connectors,
    run_connector_capture,
    set_connector_enabled,
    update_connector_settings,
)
from .cos_actions import (
    apply_action,
    decide_action,
    load_action,
    propose_action,
    recent_actions,
    record_action_audit,
    revert_action,
    row_to_action,
)
from .cos_audit import COS_AUDIT_CONFIGURED_NOTE, COS_AUDIT_STUB_NOTE, run_sampled_audit
from .cos_policy import active_policy_rules, active_policy_version, human_policy_reason
from .db import connection, dumps
from .gardener import (
    deterministic_entity_candidates,
    propose_gardener_action,
)
from .app_migration import (
    build_migration_plan,
    create_runtime_backup,
    install_runtime_shims,
    retire_launch_agents,
)
from .mcp_tools import MCP_TOOL_NAMES, call_mcp_tool
from .paths import BrainPaths
from .scheduler.launchd import LaunchdScheduler
from .service import BrainService, row_to_memory
from .setup_wizard import build_setup_plan
from .util import new_id, now_iso
from .wiki import (
    ALLOWED_PAGE_TYPES,
    ALLOWED_STATUSES,
    GENERATED_MARKER,
    lint_wiki,
    parse_frontmatter,
)
from .wiki_facts import (
    answer_open_question,
    create_confirmed_page_fact,
    managed_fact_page_review,
    reconcile_open_fact_questions,
    regenerate_managed_fact_page,
    revert_wiki_page_snapshot,
    row_to_fact,
    row_to_question,
    wiki_fact_dashboard,
)


LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


class BadRequestError(ValueError):
    pass


class NotFoundError(ValueError):
    pass


class BrainUIServer(ThreadingHTTPServer):
    paths: BrainPaths
    token: str
    serve_static: bool
    daemon_version: str | None
    daemon_started_at: str | None
    daemon_scheduler: Any | None
    daemon_shutdown_enabled: bool


class BrainUIHandler(BaseHTTPRequestHandler):
    server: BrainUIServer

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if not parsed.path.startswith("/api/"):
            self.dispatch_static_get(parsed.path)
            return
        if not self.authorized():
            self.write_json(
                {"error": "missing or invalid bearer token"},
                status=HTTPStatus.UNAUTHORIZED,
            )
            return
        self.dispatch_get(parsed.path, parse_qs(parsed.query))

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if not parsed.path.startswith("/api/"):
            self.write_json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)
            return
        if not self.authorized():
            self.write_json(
                {"error": "missing or invalid bearer token"},
                status=HTTPStatus.UNAUTHORIZED,
            )
            return
        self.dispatch_post(parsed.path)

    def do_PUT(self) -> None:
        parsed = urlparse(self.path)
        if not parsed.path.startswith("/api/"):
            self.write_json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)
            return
        if not self.authorized():
            self.write_json(
                {"error": "missing or invalid bearer token"},
                status=HTTPStatus.UNAUTHORIZED,
            )
            return
        self.dispatch_put(parsed.path)

    def log_message(self, format: str, *args: Any) -> None:
        return

    def authorized(self) -> bool:
        header = self.headers.get("Authorization", "")
        return header == f"Bearer {self.server.token}"

    def dispatch_get(self, path: str, query: dict[str, list[str]]) -> None:
        try:
            if path == "/api/health":
                self.write_json(ui_health(self.server))
            elif path == "/api/version":
                self.write_json(ui_version(self.server))
            elif path == "/api/scheduler":
                self.write_json(ui_scheduler(self.server))
            elif path == "/api/connectors":
                self.write_json(list_connectors(self.server.paths))
            elif path.startswith("/api/connectors/"):
                connector_id = path.removeprefix("/api/connectors/").strip("/")
                self.write_json(get_connector(self.server.paths, connector_id))
            elif path == "/api/migration":
                self.write_json(ui_migration_plan(self.server.paths, query))
            elif path == "/api/status":
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
                self.write_json(ui_memory_detail(self.server.paths, memory_id))
            elif path == "/api/digest":
                self.write_json(ui_digest(self.server.paths, query))
            elif path == "/api/queue":
                self.write_json(ui_queue(self.server.paths, query))
            elif path == "/api/search":
                self.write_json(ui_search(self.server.paths, query))
            elif path == "/api/entities":
                self.write_json(ui_entities(self.server.paths, query))
            elif path.startswith("/api/entities/"):
                entity_id = path.removeprefix("/api/entities/").strip("/")
                self.write_json(ui_entity_detail(self.server.paths, entity_id))
            elif path == "/api/wiki/pages":
                self.write_json(ui_wiki_pages(self.server.paths, query))
            elif path == "/api/wiki/page":
                self.write_json(ui_wiki_page(self.server.paths, query))
            elif path == "/api/wiki/facts/page":
                self.write_json(ui_wiki_fact_page_review(self.server.paths, query))
            elif path == "/api/wiki/facts":
                self.write_json(ui_wiki_fact_dashboard(self.server.paths))
            elif path == "/api/cos/policy":
                self.write_json(ui_cos_policy(self.server.paths))
            elif path == "/api/cos/actions":
                self.write_json(ui_cos_actions(self.server.paths, query))
            elif path == "/api/cos/review":
                self.write_json(ui_cos_review(self.server.paths, query))
            elif path == "/api/cos/contracts":
                self.write_json(ui_cos_contracts(self.server.paths))
            elif path == "/api/cos/audit":
                self.write_json(ui_cos_audit_status(self.server.paths))
            elif path == "/api/ops/runs":
                self.write_json(ui_ops_runs(self.server.paths))
            else:
                self.write_json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)
        except BadRequestError as exc:
            self.write_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
        except NotFoundError as exc:
            self.write_json({"error": str(exc)}, status=HTTPStatus.NOT_FOUND)
        except ValueError as exc:
            self.write_json({"error": str(exc)}, status=HTTPStatus.NOT_FOUND)
        except Exception as exc:
            self.write_json(
                {"error": str(exc)}, status=HTTPStatus.INTERNAL_SERVER_ERROR
            )

    def dispatch_post(self, path: str) -> None:
        try:
            parts = [part for part in path.removeprefix("/api/").split("/") if part]
            if parts == ["shutdown"]:
                self.write_json(ui_shutdown(self.server))
            elif len(parts) == 2 and parts[0] == "mcp":
                payload = self.read_json_body()
                self.write_json(ui_mcp_tool(self.server.paths, parts[1], payload))
            elif parts == ["migration", "backup"]:
                payload = self.read_json_body()
                self.write_json(ui_migration_backup(self.server.paths, payload))
            elif parts == ["migration", "shims"]:
                payload = self.read_json_body()
                self.write_json(ui_migration_shims(payload))
            elif parts == ["migration", "launch-agents", "retire"]:
                payload = self.read_json_body()
                self.write_json(ui_migration_retire_launch_agents(payload))
            elif parts == ["scheduler", "run"]:
                payload = self.read_json_body()
                self.write_json(ui_scheduler_run(self.server, payload))
            elif parts == ["scheduler", "pause"]:
                payload = self.read_json_body()
                self.write_json(ui_scheduler_pause(self.server, payload))
            elif parts == ["scheduler", "resume"]:
                self.write_json(ui_scheduler_resume(self.server))
            elif len(parts) == 4 and parts[:2] == ["scheduler", "jobs"] and parts[3] in {"enable", "disable"}:
                self.write_json(
                    ui_scheduler_enable(
                        self.server,
                        parts[2],
                        enabled=parts[3] == "enable",
                    )
                )
            elif len(parts) == 3 and parts[0] == "connectors" and parts[2] in {"enable", "disable"}:
                self.write_json(
                    set_connector_enabled(
                        self.server.paths,
                        parts[1],
                        enabled=parts[2] == "enable",
                    )
                )
            elif len(parts) == 3 and parts[0] == "connectors" and parts[2] == "run":
                self.write_json(
                    run_connector_capture(
                        self.server.paths,
                        connector_ids=[parts[1]],
                        respect_enabled=False,
                        respect_cadence=False,
                    ).as_dict()
                )
            elif parts == ["retrieve"]:
                payload = self.read_json_body()
                self.write_json(ui_retrieve(self.server.paths, payload))
            elif parts == ["queue", "undo"]:
                payload = self.read_json_body()
                self.write_json(ui_queue_undo(self.server.paths, payload))
            elif len(parts) == 3 and parts[0] == "queue" and parts[2] == "decision":
                payload = self.read_json_body()
                self.write_json(ui_queue_decision(self.server.paths, parts[1], payload))
            elif parts == ["entities", "merge"]:
                payload = self.read_json_body()
                self.write_json(ui_entities_merge(self.server.paths, payload))
            elif len(parts) == 3 and parts[0] == "actions" and parts[2] == "revert":
                self.write_json(ui_revert_action(self.server.paths, parts[1]))
            elif len(parts) == 4 and parts[:2] == ["wiki", "facts"] and parts[3] == "confirm":
                self.write_json(ui_confirm_fact(self.server.paths, parts[2]))
            elif len(parts) == 4 and parts[:2] == ["wiki", "facts"] and parts[3] == "flag":
                payload = self.read_json_body()
                self.write_json(ui_flag_fact(self.server.paths, parts[2], payload))
            elif parts == ["wiki", "page"]:
                payload = self.read_json_body()
                self.write_json(ui_save_wiki_page(self.server.paths, payload))
            elif parts[:2] == ["wiki", "questions"]:
                self.dispatch_wiki_question_post(parts)
            elif parts[:2] == ["wiki", "facts"]:
                self.dispatch_wiki_fact_post(parts)
            elif parts[:2] == ["cos", "questions"]:
                self.dispatch_cos_question_post(parts)
            elif parts[:2] == ["cos", "contracts"]:
                payload = self.read_json_body()
                self.write_json(ui_generate_cos_contracts(self.server.paths, payload))
            elif parts[:2] == ["cos", "audit"]:
                payload = self.read_json_body()
                self.write_json(ui_run_cos_audit(self.server.paths, payload))
            elif len(parts) == 3 and parts[0] == "memory":
                _, memory_id, action = parts
                svc = service(self.server.paths)
                if action == "approve":
                    self.write_json(
                        enrich_memory_detail(
                            self.server.paths, svc.approve_memory(memory_id)
                        )
                    )
                elif action == "reject":
                    payload = self.read_json_body()
                    self.write_json(
                        enrich_memory_detail(
                            self.server.paths,
                            svc.reject_memory(
                                memory_id, str(payload.get("reason") or "")
                            ),
                        )
                    )
                elif action == "archive":
                    self.write_json(
                        enrich_memory_detail(
                            self.server.paths, svc.archive_memory(memory_id)
                        )
                    )
                else:
                    self.write_json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)
            else:
                self.write_json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)
        except BadRequestError as exc:
            self.write_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
        except NotFoundError as exc:
            self.write_json({"error": str(exc)}, status=HTTPStatus.NOT_FOUND)
        except ValueError as exc:
            self.write_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            self.write_json(
                {"error": str(exc)}, status=HTTPStatus.INTERNAL_SERVER_ERROR
            )

    def dispatch_put(self, path: str) -> None:
        try:
            parts = [part for part in path.removeprefix("/api/").split("/") if part]
            if len(parts) == 3 and parts[0] == "connectors" and parts[2] == "settings":
                payload = self.read_json_body()
                settings = payload.get("settings") if isinstance(payload.get("settings"), dict) else payload
                self.write_json(update_connector_settings(self.server.paths, parts[1], settings))
                return
            self.write_json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)
        except BadRequestError as exc:
            self.write_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
        except NotFoundError as exc:
            self.write_json({"error": str(exc)}, status=HTTPStatus.NOT_FOUND)
        except ValueError as exc:
            self.write_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            self.write_json(
                {"error": str(exc)}, status=HTTPStatus.INTERNAL_SERVER_ERROR
            )

    def dispatch_wiki_question_post(self, parts: list[str]) -> None:
        if len(parts) == 4 and parts[3] == "answer":
            payload = self.read_json_body()
            self.write_json(
                ui_answer_wiki_question(self.server.paths, parts[2], payload)
            )
            return
        self.write_json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)

    def dispatch_cos_question_post(self, parts: list[str]) -> None:
        if len(parts) == 4 and parts[3] == "apply-action":
            payload = self.read_json_body()
            self.write_json(
                ui_apply_cos_question_action(self.server.paths, parts[2], payload)
            )
            return
        if len(parts) == 4 and parts[3] == "dismiss":
            payload = self.read_json_body()
            self.write_json(ui_dismiss_cos_question(self.server.paths, parts[2], payload))
            return
        self.write_json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)

    def dispatch_wiki_fact_post(self, parts: list[str]) -> None:
        if parts == ["wiki", "facts", "reconcile"]:
            payload = self.read_json_body()
            self.write_json(ui_reconcile_wiki_facts(self.server.paths, payload))
            return
        if parts == ["wiki", "facts", "pages", "regenerate"]:
            payload = self.read_json_body()
            self.write_json(ui_regenerate_wiki_fact_page(self.server.paths, payload))
            return
        if parts == ["wiki", "facts", "pages", "revert"]:
            payload = self.read_json_body()
            self.write_json(ui_revert_wiki_fact_page(self.server.paths, payload))
            return
        if parts == ["wiki", "facts", "corrections"]:
            payload = self.read_json_body()
            self.write_json(ui_create_wiki_fact_correction(self.server.paths, payload))
            return
        self.write_json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)

    def read_json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        raw = self.rfile.read(length).decode("utf-8")
        return json.loads(raw)

    def dispatch_static_get(self, path: str) -> None:
        if not self.server.serve_static:
            self.write_json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)
            return
        if path in {"", "/"}:
            self.write_static("index.html")
            return
        if path.startswith("/ui/"):
            self.write_static(path.removeprefix("/ui/"))
            return
        self.write_json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)

    def write_static(self, relative_path: str) -> None:
        parts = [part for part in relative_path.split("/") if part]
        if not parts or any(part == ".." for part in parts):
            self.write_json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)
            return
        target = resources.files("pkm_brain").joinpath("ui_static", *parts)
        if not target.is_file():
            self.write_json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)
            return
        encoded = target.read_bytes()
        mime = mimetypes.guess_type(parts[-1])[0] or "application/octet-stream"
        if mime.startswith("text/") or mime == "application/javascript":
            mime = f"{mime}; charset=utf-8"
        self.send_response(HTTPStatus.OK.value)
        self.send_header("Content-Type", mime)
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def write_html(self, body: str, status: HTTPStatus = HTTPStatus.OK) -> None:
        encoded = body.encode("utf-8")
        self.send_response(status.value)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def write_json(
        self, payload: dict[str, Any] | list[Any], status: HTTPStatus = HTTPStatus.OK
    ) -> None:
        encoded = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
        self.send_response(status.value)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


def create_ui_server(
    paths: BrainPaths,
    host: str,
    port: int,
    token: str | None = None,
    *,
    serve_static: bool = True,
) -> BrainUIServer:
    token = token or ensure_ui_token(paths)
    server = BrainUIServer((host, port), BrainUIHandler)
    server.paths = paths
    server.token = token
    server.serve_static = serve_static
    server.daemon_version = None
    server.daemon_started_at = None
    server.daemon_scheduler = None
    server.daemon_shutdown_enabled = False
    return server


def ui_health(server: BrainUIServer) -> dict[str, Any]:
    host, port = server.server_address
    return {
        "ok": True,
        "version": server.daemon_version or package_version(),
        "home": str(server.paths.home),
        "pid": os.getpid(),
        "host": str(host),
        "port": int(port),
        "started_at": server.daemon_started_at,
        "schema_version": current_schema_version(),
    }


def ui_version(server: BrainUIServer) -> dict[str, Any]:
    return {
        "version": server.daemon_version or package_version(),
        "schema_version": current_schema_version(),
    }


def ui_scheduler(server: BrainUIServer) -> dict[str, Any]:
    scheduler = server.daemon_scheduler
    if scheduler is None:
        raise BadRequestError("scheduler is not available")
    return scheduler.as_dict()


def ui_scheduler_run(server: BrainUIServer, payload: dict[str, Any]) -> dict[str, Any]:
    scheduler = server.daemon_scheduler
    if scheduler is None:
        raise BadRequestError("scheduler is not available")
    job_id = str(payload.get("job_id") or "").strip()
    if not job_id:
        raise BadRequestError("job_id is required")
    return scheduler.run_now(job_id)


def ui_scheduler_pause(server: BrainUIServer, payload: dict[str, Any]) -> dict[str, Any]:
    scheduler = server.daemon_scheduler
    if scheduler is None:
        raise BadRequestError("scheduler is not available")
    seconds = int(payload.get("seconds") or 0)
    return scheduler.pause(seconds)


def ui_scheduler_resume(server: BrainUIServer) -> dict[str, Any]:
    scheduler = server.daemon_scheduler
    if scheduler is None:
        raise BadRequestError("scheduler is not available")
    return scheduler.resume()


def ui_scheduler_enable(server: BrainUIServer, job_id: str, *, enabled: bool) -> dict[str, Any]:
    scheduler = server.daemon_scheduler
    if scheduler is None:
        raise BadRequestError("scheduler is not available")
    return scheduler.set_enabled(job_id, enabled)


def ui_shutdown(server: BrainUIServer) -> dict[str, Any]:
    if not server.daemon_shutdown_enabled:
        raise BadRequestError("shutdown is only available for brain daemon")
    threading.Thread(target=server.shutdown, name="brain-daemon-shutdown", daemon=True).start()
    return {"ok": True, "shutting_down": True}


def ui_mcp_tool(paths: BrainPaths, tool_name: str, payload: dict[str, Any]) -> dict[str, Any] | list[Any]:
    if tool_name not in MCP_TOOL_NAMES:
        raise NotFoundError(f"unknown MCP tool: {tool_name}")
    result = call_mcp_tool(service(paths), tool_name, payload)
    if isinstance(result, (dict, list)):
        return result
    return {"result": result}


def ui_migration_plan(paths: BrainPaths, query: dict[str, list[str]]) -> dict[str, Any]:
    return build_migration_plan(
        paths,
        app_support_dir=path_from_query(query, "app_support_dir"),
        launch_agents_dir=path_from_query(query, "launch_agents_dir"),
    )


def ui_migration_backup(paths: BrainPaths, payload: dict[str, Any]) -> dict[str, Any]:
    return create_runtime_backup(paths, app_support_dir=path_from_payload(payload, "app_support_dir"))


def ui_migration_shims(payload: dict[str, Any]) -> dict[str, Any]:
    return install_runtime_shims(app_support_dir=path_from_payload(payload, "app_support_dir"))


def ui_migration_retire_launch_agents(payload: dict[str, Any]) -> dict[str, Any]:
    return retire_launch_agents(
        app_support_dir=path_from_payload(payload, "app_support_dir"),
        launch_agents_dir=path_from_payload(payload, "launch_agents_dir"),
        dry_run=bool(payload.get("dry_run", True)),
    )


def path_from_payload(payload: dict[str, Any], key: str) -> Path | None:
    value = payload.get(key)
    return Path(str(value)).expanduser() if value else None


def path_from_query(query: dict[str, list[str]], key: str) -> Path | None:
    values = query.get(key) or []
    return Path(values[0]).expanduser() if values else None


def package_version() -> str:
    from importlib import metadata

    try:
        return metadata.version("pkm-brain")
    except metadata.PackageNotFoundError:
        return "0.0.0+local"


def current_schema_version() -> int:
    from .migrations import MIGRATIONS

    return max((version for version, _name, _fn in MIGRATIONS), default=0)


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
            raise ValueError(
                f"{warning}; pass --i-understand-this-binds-to-lan to continue"
            )
    return {
        "host": host,
        "default_host": "127.0.0.1",
        "allowed": True,
        "warning": warning,
    }


def ui_status(paths: BrainPaths) -> dict[str, Any]:
    svc = service(paths)
    svc.init_workspace()
    return {
        "doctor": svc.doctor(),
        "sync": safe_call(svc.sync_status),
        "index": index_status(paths, svc),
        "memory": audit_memories(paths),
        "retrieval_surfaces": retrieval_surface_status(paths),
    }


def retrieval_surface_status(paths: BrainPaths) -> list[dict[str, Any]]:
    wiki_pages = sum(1 for path in paths.wiki.rglob("*.md")) if paths.wiki.exists() else 0
    with connection(paths.sqlite_path) as conn:
        chunk_rows = scalar_count(conn, "SELECT COUNT(*) FROM chunks")
        fact_index_rows = scalar_count(
            conn,
            "SELECT COUNT(*) FROM retrieval_fts WHERE kind = 'fact'",
            table="retrieval_fts",
        )
        searchable_facts = scalar_count(
            conn,
            """
            SELECT COUNT(*)
            FROM facts
            WHERE status IN ('active', 'conflicted')
              AND COALESCE(truth_confidence, confidence, 0) >= 0.5
            """,
            table="facts",
        )
        active_memories = scalar_count(
            conn, "SELECT COUNT(*) FROM memories WHERE status = 'active'", table="memories"
        )
        proposed_memories = scalar_count(
            conn, "SELECT COUNT(*) FROM memories WHERE status = 'proposed'", table="memories"
        )
        actions = scalar_count(conn, "SELECT COUNT(*) FROM cos_actions", table="cos_actions")
    return [
        {
            "surface": "Fact ledger",
            "searched": True,
            "count": searchable_facts,
            "indexed": fact_index_rows,
            "role": "authoritative claims",
            "details": "Active and contested facts that pass the truth-confidence floor. Inactive and superseded facts are excluded.",
        },
        {
            "surface": "Raw source chunks",
            "searched": True,
            "count": chunk_rows,
            "indexed": chunk_rows,
            "role": "evidence fallback and citations",
            "details": "FTS/vector chunk candidates from ingested source documents. Agent-session logs are downranked for non-agent queries.",
        },
        {
            "surface": "Wiki pages",
            "searched": True,
            "count": wiki_pages,
            "indexed": wiki_pages,
            "role": "compiled projections",
            "details": "Markdown wiki pages are searched as page-level context. Managed pages are deterministic projections from active facts.",
        },
        {
            "surface": "Memories",
            "searched": True,
            "count": active_memories + proposed_memories,
            "indexed": active_memories + proposed_memories,
            "role": "curated memory hints",
            "details": f"{active_memories} active and {proposed_memories} proposed memories can be returned; proposed memories are lower-trust candidates.",
        },
        {
            "surface": "CoS action ledger",
            "searched": False,
            "count": actions,
            "indexed": 0,
            "role": "governance/audit",
            "details": "Actions, policy, contracts, and audits govern writes. They are not evidence returned by retrieval.",
        },
    ]


def scalar_count(conn: Any, query: str, table: str | None = None) -> int:
    if table and not ui_table_exists(conn, table):
        return 0
    row = conn.execute(query).fetchone()
    return int(row[0] if row else 0)


def ui_table_exists(conn: Any, table: str) -> bool:
    return bool(
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type IN ('table', 'view') AND name = ?",
            (table,),
        ).fetchone()
    )


def ui_column_exists(conn: Any, table: str, column: str) -> bool:
    if not ui_table_exists(conn, table):
        return False
    return column in {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})")}


def row_to_plain_dict(row: Any) -> dict[str, Any]:
    output = dict(row)
    for key, value in list(output.items()):
        if isinstance(value, str) and value[:1] in {"{", "["}:
            output[key] = json_loads(value, value)
    return output


def ui_memories(paths: BrainPaths, query: dict[str, list[str]]) -> dict[str, Any]:
    svc = service(paths)
    memories = svc.list_memories(
        status=first(query, "status"),
        scope=first(query, "scope"),
        memory_type=first(query, "memory_type"),
        project=first(query, "project"),
    )
    return {"memories": memories, "count": len(memories)}


def ui_memory_detail(paths: BrainPaths, memory_id: str) -> dict[str, Any]:
    try:
        memory = service(paths).get_memory(memory_id)
    except ValueError as exc:
        raise NotFoundError(str(exc)) from exc
    return enrich_memory_detail(paths, memory)


def enrich_memory_detail(paths: BrainPaths, memory: dict[str, Any]) -> dict[str, Any]:
    output = dict(memory)
    output["source_documents"] = source_document_summaries(
        paths, output.get("source_ids") or []
    )
    audit = audit_memories(paths)
    memory_id = str(output.get("id") or "")
    output["audit"] = {
        "errors": [
            error
            for error in audit.get("errors", [])
            if error.startswith(f"{memory_id}:")
        ],
        "warnings": [
            warning
            for warning in audit.get("warnings", [])
            if warning.startswith(f"{memory_id}:")
        ],
    }
    return output


def ui_digest(paths: BrainPaths, query: dict[str, list[str]]) -> dict[str, Any]:
    svc = service(paths)
    svc.init_workspace()
    since = first(query, "since")
    index = safe_call(lambda: index_status(paths, svc))
    jobs = ui_jobs_status()
    sync = safe_call(svc.sync_status)
    audit = ui_cos_audit_status(paths)
    with connection(paths.sqlite_path) as conn:
        latest_run = compact_run_event(latest_row(conn, "automation_runs", "started_at"))
        facts_by_page = digest_facts_by_page(conn, since)
        reverts = digest_reverts(conn, since)
        demotions = digest_demotions(conn, since)
        eval_transitions = digest_eval_transitions(conn, since)
        counts = queue_counts(conn)
    pulse = [
        pulse_chip(
            "nightly",
            latest_run.get("status") if latest_run else None,
            latest_run.get("finished_at") or latest_run.get("started_at") if latest_run else None,
            href="#/ops/runs",
            bad_statuses={"failed", "error", "crashed"},
        ),
        {
            "key": "evals",
            "label": "evals",
            "state": "warn" if audit.get("counts", {}).get("sampled_bad") else "ok",
            "value": "findings"
            if audit.get("counts", {}).get("sampled_bad")
            else "all",
            "href": "#/ops/audit",
        },
        {
            "key": "index",
            "label": "index",
            "state": "bad" if index.get("error") else "ok",
            "value": index.get("embedding_provider")
            or index.get("embeddings_provider")
            or "ready",
            "href": "#/ops/index",
        },
        {
            "key": "agents",
            "label": "agents",
            "state": "warn" if jobs.get("error") else "ok",
            "value": f"{len(jobs.get('jobs') or [])} jobs"
            if jobs.get("jobs")
            else "local",
            "href": "#/ops/runs",
        },
    ]
    if sync.get("configured") is not False:
        pulse.append(
            {
                "key": "sync",
                "label": "sync",
                "state": "bad" if sync.get("error") else "ok",
                "value": sync.get("role") or "configured",
                "href": "#/ops/sync",
            }
        )
    return {
        "generated_at": now_iso(),
        "since": since,
        "pulse": pulse,
        "latest_run": latest_run,
        "facts_by_page": facts_by_page,
        "reverts": reverts,
        "demotions": demotions,
        "eval_transitions": eval_transitions,
        "queue_counts": counts,
        "raw": {
            "index": index,
            "jobs": jobs,
            "sync": sync,
            "audit": {
                "status": audit.get("status"),
                "mode": audit.get("mode"),
                "counts": audit.get("counts"),
                "note": audit.get("note"),
            },
        },
    }


def latest_row(conn: Any, table: str, order_column: str) -> dict[str, Any] | None:
    if not ui_table_exists(conn, table):
        return None
    row = conn.execute(
        f"SELECT * FROM {table} ORDER BY {order_column} DESC LIMIT 1"
    ).fetchone()
    if not row:
        return None
    return row_to_plain_dict(row)


def pulse_chip(
    key: str,
    status: Any,
    timestamp: Any,
    *,
    href: str,
    bad_statuses: set[str],
) -> dict[str, Any]:
    normalized = str(status or "unknown").casefold()
    if normalized in bad_statuses:
        state = "bad"
    elif normalized in {"", "unknown", "running", "pending"}:
        state = "warn"
    else:
        state = "ok"
    return {
        "key": key,
        "label": key,
        "state": state,
        "value": normalized or "unknown",
        "timestamp": timestamp,
        "href": href,
    }


def digest_facts_by_page(conn: Any, since: str | None) -> list[dict[str, Any]]:
    if not ui_table_exists(conn, "facts"):
        return []
    where = ""
    params: list[Any] = []
    if since:
        where = "WHERE COALESCE(created_at, last_seen_at, observed_at) > ?"
        params.append(since)
    rows = conn.execute(
        f"""
        SELECT COALESCE(page_hint, entity_key, '(unrouted)') AS page_hint,
               COUNT(*) AS count,
               MAX(COALESCE(created_at, last_seen_at, observed_at)) AS latest_at
        FROM facts
        {where}
        GROUP BY COALESCE(page_hint, entity_key, '(unrouted)')
        ORDER BY count DESC, latest_at DESC
        LIMIT 40
        """,
        params,
    )
    return [
        {
            "page_hint": str(row["page_hint"]),
            "count": int(row["count"]),
            "latest_at": row["latest_at"],
        }
        for row in rows
    ]


def digest_reverts(conn: Any, since: str | None) -> list[dict[str, Any]]:
    if not ui_table_exists(conn, "cos_actions") or not ui_column_exists(
        conn, "cos_actions", "reverted_at"
    ):
        return []
    params: list[Any] = []
    where = "WHERE status = 'reverted'"
    if since:
        where += " AND reverted_at > ?"
        params.append(since)
    return [
        compact_action_event(row_to_action(row))
        for row in conn.execute(
            f"""
            SELECT *
            FROM cos_actions
            {where}
            ORDER BY reverted_at DESC
            LIMIT 25
            """,
            params,
        )
    ]


def digest_demotions(conn: Any, since: str | None) -> list[dict[str, Any]]:
    if not ui_table_exists(conn, "cos_actions"):
        return []
    params: list[Any] = []
    where = "WHERE audit_status = 'sampled_bad'"
    if since and ui_column_exists(conn, "cos_actions", "applied_at"):
        where += " AND COALESCE(applied_at, created_at) > ?"
        params.append(since)
    return [
        compact_action_event(row_to_action(row))
        for row in conn.execute(
            f"""
            SELECT *
            FROM cos_actions
            {where}
            ORDER BY COALESCE(applied_at, created_at) DESC
            LIMIT 25
            """,
            params,
        )
    ]


def compact_action_event(action: dict[str, Any]) -> dict[str, Any]:
    evidence = action.get("evidence_json") or {}
    audits = evidence.get("audits") if isinstance(evidence, dict) else []
    latest_audit = audits[-1] if isinstance(audits, list) and audits else {}
    metadata = latest_audit.get("metadata") if isinstance(latest_audit, dict) else {}
    return {
        "id": action.get("id"),
        "action_type": action.get("action_type"),
        "status": action.get("status"),
        "audit_status": action.get("audit_status"),
        "risk_tier": action.get("risk_tier"),
        "created_at": action.get("created_at"),
        "applied_at": action.get("applied_at"),
        "reverted_at": action.get("reverted_at"),
        "target_fact_ids": action.get("target_fact_ids") or [],
        "target_page_paths": action.get("target_page_paths") or [],
        "reason": compact_text(
            (metadata or {}).get("rationale")
            or evidence.get("reason")
            if isinstance(evidence, dict)
            else "",
            240,
        ),
    }


def compact_run_event(run: dict[str, Any] | None) -> dict[str, Any] | None:
    if run is None:
        return None
    summary = run.get("summary") if isinstance(run.get("summary"), dict) else {}
    compact_summary: dict[str, Any] = {}
    for key in [
        "status",
        "documents_discovered",
        "documents_changed",
        "facts_created",
        "facts_updated",
        "actions_created",
        "actions_applied",
        "questions_created",
        "errors",
        "warnings",
    ]:
        if key in summary:
            value = summary[key]
            if isinstance(value, list):
                compact_summary[key] = len(value)
            else:
                compact_summary[key] = value
    return {
        "id": run.get("id"),
        "job_name": run.get("job_name"),
        "status": run.get("status"),
        "started_at": run.get("started_at"),
        "finished_at": run.get("finished_at"),
        "error": compact_text(run.get("error"), 240),
        "summary": compact_summary,
    }


def digest_eval_transitions(conn: Any, since: str | None) -> list[dict[str, Any]]:
    if not ui_table_exists(conn, "automation_runs"):
        return []
    params: list[Any] = []
    where = "WHERE job_name LIKE '%eval%'"
    if since:
        where += " AND COALESCE(finished_at, started_at) > ?"
        params.append(since)
    return [
        row_to_plain_dict(row)
        for row in conn.execute(
            f"""
            SELECT *
            FROM automation_runs
            {where}
            ORDER BY COALESCE(finished_at, started_at) DESC
            LIMIT 20
            """,
            params,
        )
    ]


def queue_counts(conn: Any) -> dict[str, Any]:
    counts: dict[str, int] = {}
    raw: dict[str, int] = {}
    if ui_table_exists(conn, "open_questions"):
        for row in conn.execute(
            """
            SELECT kind, COUNT(*) AS count
            FROM open_questions
            WHERE status IN ('open', 'needs_human')
            GROUP BY kind
            """
        ):
            kind = str(row["kind"])
            count = int(row["count"])
            raw[kind] = count
            counts[queue_group_for_kind(kind)] = counts.get(queue_group_for_kind(kind), 0) + count
    if ui_table_exists(conn, "memories"):
        count = scalar_count(
            conn,
            "SELECT COUNT(*) FROM memories WHERE status = 'proposed'",
            table="memories",
        )
        if count:
            counts["memories"] = counts.get("memories", 0) + count
            raw["proposed_memory"] = count
    if ui_table_exists(conn, "cos_actions"):
        question_action_ids = active_question_action_ids(conn)
        proposed = queue_action_count(conn, exclude_action_ids=question_action_ids)
        if proposed:
            counts["topology"] = counts.get("topology", 0) + proposed
            raw["proposed_action"] = proposed
        audit = queue_audit_count(conn)
        if audit:
            counts["audit"] = counts.get("audit", 0) + audit
            raw["audit_flagged"] = audit
    total = sum(counts.values())
    return {"total": total, "by_kind": dict(sorted(counts.items())), "raw": dict(sorted(raw.items()))}


def queue_group_for_kind(kind: str, action_type: str | None = None) -> str:
    normalized = str(kind or "").strip()
    if normalized in {"fact_conflict_review", "conflict"}:
        return "conflicts"
    if normalized == "unrouted_fact":
        return "unrouted"
    if normalized == "document_extraction_anomaly":
        return "anomalies"
    if normalized == "proposed_memory":
        return "memories"
    if normalized == "audit_flagged":
        return "audit"
    if normalized == "proposed_action":
        return "topology"
    if normalized == "policy_escalation" and action_type in TOPOLOGY_ACTION_TYPES:
        return "topology"
    return normalized or "other"


TOPOLOGY_ACTION_TYPES = {
    "page_merge",
    "page_split",
    "rename_page",
    "archive_page",
    "rehome_fact",
    "edit_contract",
    "entity_merge",
    "entity_split",
    "synthesize_page",
}


class QueueBuildContext:
    def __init__(self, paths: BrainPaths, conn: Any):
        self.paths = paths
        self.conn = conn
        self._active_pages: list[dict[str, Any]] | None = None
        self._action_cache: dict[str, dict[str, Any]] = {}
        self._fact_cache: dict[str, dict[str, Any]] = {}
        self._source_document_cache: dict[str, dict[str, Any] | None] = {}

    def active_pages(self) -> list[dict[str, Any]]:
        if self._active_pages is None:
            self._active_pages = queue_active_pages(self.paths, self.conn)
        return self._active_pages

    def action(self, action_id: str) -> dict[str, Any]:
        if action_id not in self._action_cache:
            action = load_action(self.conn, action_id)
            self._action_cache[action_id] = action
        return self._action_cache[action_id]

    def facts(self, fact_ids: list[str]) -> dict[str, dict[str, Any]]:
        missing = [fact_id for fact_id in fact_ids if fact_id not in self._fact_cache]
        if missing and ui_table_exists(self.conn, "facts"):
            placeholders = ",".join("?" for _ in missing)
            rows = self.conn.execute(
                f"SELECT * FROM facts WHERE id IN ({placeholders})",
                missing,
            )
            for row in rows:
                self._fact_cache[str(row["id"])] = row_to_fact(row)
        return {
            fact_id: self._fact_cache[fact_id]
            for fact_id in fact_ids
            if fact_id in self._fact_cache
        }

    def source_documents(self, source_ids: list[str]) -> list[dict[str, Any]]:
        document_ids = [
            str(source_id).removeprefix("document:")
            for source_id in source_ids
            if str(source_id).startswith("document:")
            and str(source_id).removeprefix("document:")
        ]
        missing = [
            document_id
            for document_id in document_ids
            if document_id not in self._source_document_cache
        ]
        if missing and ui_table_exists(self.conn, "documents"):
            placeholders = ",".join("?" for _ in missing)
            rows = self.conn.execute(
                f"""
                SELECT id, title, source_type, source_path, raw_path, ingested_at
                FROM documents
                WHERE id IN ({placeholders})
                """,
                missing,
            )
            found = {str(row["id"]): dict(row) for row in rows}
            for document_id in missing:
                self._source_document_cache[document_id] = found.get(document_id)
        documents = []
        for document_id in document_ids:
            row = self._source_document_cache.get(document_id)
            if not row:
                continue
            documents.append(
                {
                    "id": row["id"],
                    "source_id": f"document:{row['id']}",
                    "title": row["title"],
                    "source_type": row["source_type"],
                    "source_path": row["source_path"],
                    "raw_path": row["raw_path"],
                    "ingested_at": row["ingested_at"],
                }
            )
        return documents


def queue_context_paths(context: BrainPaths | QueueBuildContext) -> BrainPaths:
    return context.paths if isinstance(context, QueueBuildContext) else context


def active_question_action_ids(conn: Any) -> set[str]:
    if not ui_table_exists(conn, "open_questions") or not ui_column_exists(
        conn, "open_questions", "action_id"
    ):
        return set()
    return {
        str(row["action_id"])
        for row in conn.execute(
            """
            SELECT action_id
            FROM open_questions
            WHERE action_id IS NOT NULL
              AND status IN ('open', 'needs_human')
            """
        )
        if str(row["action_id"] or "").strip()
    }


def queue_action_rows(
    conn: Any, *, exclude_action_ids: set[str], limit: int
) -> list[dict[str, Any]]:
    if not ui_table_exists(conn, "cos_actions"):
        return []
    placeholders = ",".join("?" for _ in exclude_action_ids)
    exclude = f"AND id NOT IN ({placeholders})" if exclude_action_ids else ""
    params: list[Any] = list(exclude_action_ids)
    rows = conn.execute(
        f"""
        SELECT *
        FROM cos_actions
        WHERE status IN ('proposed', 'needs_human')
          AND action_type IN ({",".join("?" for _ in TOPOLOGY_ACTION_TYPES)})
          {exclude}
        ORDER BY
          CASE risk_tier WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END,
          created_at DESC
        LIMIT ?
        """,
        [*TOPOLOGY_ACTION_TYPES, *params, limit],
    )
    return [row_to_action(row) for row in rows]


def queue_action_count(conn: Any, *, exclude_action_ids: set[str]) -> int:
    if not ui_table_exists(conn, "cos_actions"):
        return 0
    placeholders = ",".join("?" for _ in exclude_action_ids)
    exclude = f"AND id NOT IN ({placeholders})" if exclude_action_ids else ""
    params: list[Any] = list(exclude_action_ids)
    row = conn.execute(
        f"""
        SELECT COUNT(*)
        FROM cos_actions
        WHERE status IN ('proposed', 'needs_human')
          AND action_type IN ({",".join("?" for _ in TOPOLOGY_ACTION_TYPES)})
          {exclude}
        """,
        [*TOPOLOGY_ACTION_TYPES, *params],
    ).fetchone()
    return int(row[0] if row else 0)


def queue_audit_rows(conn: Any, *, limit: int) -> list[dict[str, Any]]:
    if not ui_table_exists(conn, "cos_actions"):
        return []
    rows = conn.execute(
        """
        SELECT *
        FROM cos_actions
        WHERE audit_status = 'sampled_bad'
          AND status NOT IN ('reverted', 'rejected', 'dismissed')
        ORDER BY COALESCE(applied_at, created_at) DESC
        LIMIT ?
        """,
        (limit,),
    )
    return [row_to_action(row) for row in rows]


def queue_audit_count(conn: Any) -> int:
    if not ui_table_exists(conn, "cos_actions"):
        return 0
    row = conn.execute(
        """
        SELECT COUNT(*)
        FROM cos_actions
        WHERE audit_status = 'sampled_bad'
          AND status NOT IN ('reverted', 'rejected', 'dismissed')
        """
    ).fetchone()
    return int(row[0] if row else 0)


def queue_memory_rows(conn: Any, *, limit: int) -> list[dict[str, Any]]:
    if not ui_table_exists(conn, "memories"):
        return []
    rows = conn.execute(
        """
        SELECT *
        FROM memories
        WHERE status = 'proposed'
        ORDER BY updated_at DESC, created_at DESC
        LIMIT ?
        """,
        (limit,),
    )
    return [row_to_memory(row) for row in rows]


def ui_queue(paths: BrainPaths, query: dict[str, list[str]]) -> dict[str, Any]:
    kind_filter = first(query, "kind") or "all"
    limit = bounded_int(first(query, "limit"), default=200, minimum=1, maximum=500)
    cursor = bounded_int(first(query, "cursor"), default=0, minimum=0, maximum=100_000)
    with connection(paths.sqlite_path) as conn:
        counts = queue_counts(conn)
        total = queue_total_from_counts(counts, kind_filter)
        items = queue_items(paths, conn, kind_filter=kind_filter, limit=limit, cursor=cursor)
    next_cursor = cursor + limit if cursor + limit < total else None
    return {
        "kind": kind_filter,
        "counts": counts,
        "total": total,
        "cursor": cursor,
        "next_cursor": next_cursor,
        "items": items,
    }


def queue_items(
    paths: BrainPaths,
    conn: Any,
    *,
    kind_filter: str,
    limit: int,
    cursor: int,
) -> list[dict[str, Any]]:
    fetch_limit = cursor + limit
    descriptors: list[dict[str, Any]] = []
    if fetch_limit <= 0:
        return []
    descriptors.extend(queue_question_descriptors(conn, kind_filter, fetch_limit))
    if queue_filter_matches(kind_filter, "proposed_action", "topology"):
        question_action_ids = active_question_action_ids(conn)
        descriptors.extend(
            {
                "source_type": "action",
                "item": action,
                "sort": queue_item_sort_key(queue_action_stub(action)),
            }
            for action in queue_action_rows(
                conn, exclude_action_ids=question_action_ids, limit=fetch_limit
            )
        )
    if queue_filter_matches(kind_filter, "audit_flagged", "audit"):
        descriptors.extend(
            {
                "source_type": "audit",
                "item": action,
                "sort": queue_item_sort_key(queue_audit_stub(action)),
            }
            for action in queue_audit_rows(conn, limit=fetch_limit)
        )
    if queue_filter_matches(kind_filter, "proposed_memory", "memories"):
        descriptors.extend(
            {
                "source_type": "memory",
                "item": memory,
                "sort": queue_item_sort_key(queue_memory_stub(memory)),
            }
            for memory in queue_memory_rows(conn, limit=fetch_limit)
        )
    descriptors.sort(key=lambda descriptor: descriptor["sort"])
    ctx = QueueBuildContext(paths, conn)
    return [
        queue_item_from_descriptor(ctx, descriptor)
        for descriptor in descriptors[cursor : cursor + limit]
    ]


def queue_question_descriptors(
    conn: Any, kind_filter: str, limit: int
) -> list[dict[str, Any]]:
    if not ui_table_exists(conn, "open_questions"):
        return []
    where, params = queue_question_filter_sql(kind_filter)
    if where is None:
        return []
    rows = conn.execute(
        f"""
        SELECT *
        FROM open_questions
        WHERE status IN ('open', 'needs_human')
          {where}
        ORDER BY
          CASE kind
            WHEN 'fact_conflict_review' THEN 0
            WHEN 'conflict' THEN 1
            WHEN 'unrouted_fact' THEN 2
            WHEN 'document_extraction_anomaly' THEN 3
            WHEN 'policy_escalation' THEN 4
            ELSE 5
          END,
          CASE risk_tier WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END,
          created_at DESC
        LIMIT ?
        """,
        [*params, limit],
    )
    descriptors = []
    for row in rows:
        question = row_to_question(row)
        stub = queue_question_stub(question)
        if queue_filter_matches(
            kind_filter, str(stub.get("kind") or ""), str(stub.get("group") or "")
        ):
            descriptors.append(
                {
                    "source_type": "question",
                    "item": question,
                    "sort": queue_item_sort_key(stub),
                }
            )
    return descriptors


def queue_item_from_descriptor(
    ctx: QueueBuildContext, descriptor: dict[str, Any]
) -> dict[str, Any]:
    source_type = str(descriptor["source_type"])
    if source_type == "question":
        return queue_item_for_question(ctx, descriptor["item"])
    if source_type == "action":
        return queue_item_for_action(ctx, descriptor["item"])
    if source_type == "audit":
        return queue_item_for_audit_action(ctx, descriptor["item"])
    if source_type == "memory":
        return queue_item_for_memory(ctx, descriptor["item"])
    raise ValueError(f"unsupported queue source: {source_type}")


def queue_question_filter_sql(kind_filter: str) -> tuple[str | None, list[Any]]:
    normalized = normalize_queue_filter(kind_filter)
    if normalized == "all":
        return "", []
    if normalized == "conflicts":
        return "AND kind IN ('fact_conflict_review', 'conflict')", []
    if normalized == "unrouted":
        return "AND kind = 'unrouted_fact'", []
    if normalized == "anomalies":
        return "AND kind = 'document_extraction_anomaly'", []
    if normalized == "topology":
        return "AND kind = 'policy_escalation'", []
    question_kinds = {
        "fact_conflict_review",
        "conflict",
        "unrouted_fact",
        "document_extraction_anomaly",
        "policy_escalation",
    }
    if normalized in question_kinds:
        return "AND kind = ?", [normalized]
    return None, []


def queue_total_from_counts(counts: dict[str, Any], kind_filter: str) -> int:
    normalized = normalize_queue_filter(kind_filter)
    if normalized == "all":
        return int(counts.get("total") or 0)
    by_kind = counts.get("by_kind") or {}
    raw = counts.get("raw") or {}
    if normalized in by_kind:
        return int(by_kind.get(normalized) or 0)
    if normalized in raw:
        return int(raw.get(normalized) or 0)
    return 0


def queue_filter_matches(kind_filter: str, raw_kind: str, group: str) -> bool:
    normalized = normalize_queue_filter(kind_filter)
    if normalized in {"", "all"}:
        return True
    return normalized in {raw_kind, group}


def normalize_queue_filter(kind_filter: str) -> str:
    normalized = str(kind_filter or "all").strip()
    aliases = {
        "conflict": "conflicts",
        "fact_conflict_review": "conflicts",
        "memory": "memories",
        "proposed_memory": "memories",
        "audit-flagged": "audit",
        "audit_flagged": "audit",
    }
    return aliases.get(normalized, normalized)


def queue_item_sort_key(item: dict[str, Any]) -> tuple[int, int, str]:
    group_priority = {
        "conflicts": 0,
        "unrouted": 1,
        "topology": 2,
        "memories": 3,
        "audit": 4,
        "anomalies": 5,
    }
    risk_priority = {"high": 0, "medium": 1, "low": 2}
    return (
        group_priority.get(str(item.get("group") or ""), 9),
        risk_priority.get(str(item.get("risk_tier") or ""), 3),
        str(item.get("created_at") or ""),
    )


def queue_question_stub(question: dict[str, Any]) -> dict[str, Any]:
    action_type = str((question.get("recommended_action") or {}).get("action_type") or "")
    group = queue_group_for_kind(str(question.get("kind") or ""), action_type)
    return {
        "kind": question.get("kind"),
        "group": group,
        "created_at": question.get("created_at"),
        "risk_tier": question.get("risk_tier"),
    }


def queue_action_stub(action: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": "proposed_action",
        "group": "topology",
        "created_at": action.get("created_at"),
        "risk_tier": action.get("risk_tier"),
    }


def queue_audit_stub(action: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": "audit_flagged",
        "group": "audit",
        "created_at": action.get("applied_at") or action.get("created_at"),
        "risk_tier": action.get("risk_tier"),
    }


def queue_memory_stub(memory: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": "proposed_memory",
        "group": "memories",
        "created_at": memory.get("updated_at") or memory.get("created_at"),
        "risk_tier": "low",
    }


def queue_item_for_question(
    context: BrainPaths | QueueBuildContext, question: dict[str, Any]
) -> dict[str, Any]:
    action_type = str((question.get("recommended_action") or {}).get("action_type") or "")
    group = queue_group_for_kind(str(question.get("kind") or ""), action_type)
    candidate = question_candidate_fact(context, question)
    counterparts = question_counterpart_facts(context, question)
    summary = readable_question_summary(context, question)
    title = compact_text(
        (candidate or {}).get("statement")
        or question.get("question")
        or question.get("page_hint")
        or question.get("entity_key")
        or question.get("id"),
        120,
    )
    item = {
        "id": question["id"],
        "source_type": "question",
        "kind": question["kind"],
        "group": group,
        "title": title,
        "summary": compact_text(summary, 240),
        "created_at": question.get("created_at"),
        "status": question.get("status"),
        "risk_tier": question.get("risk_tier"),
        "page_hint": question.get("page_hint"),
        "entity_key": question.get("entity_key"),
        "action_id": question.get("action_id"),
        "question": question,
        "candidate": candidate,
        "counterparts": counterparts,
        "options": question.get("options") or [],
        "raw": question,
    }
    if group == "unrouted" and candidate:
        item["route_candidates"] = route_candidates_for_fact(context, candidate)
    return item


def readable_question_summary(
    context: BrainPaths | QueueBuildContext, question: dict[str, Any]
) -> str:
    policy_reason = readable_policy_escalation_reason(context, question)
    if policy_reason:
        return policy_reason
    return str(question.get("question") or "")


def readable_policy_escalation_reason(
    context: BrainPaths | QueueBuildContext, question: dict[str, Any]
) -> str | None:
    if question.get("kind") != "policy_escalation":
        return None
    raw_question = str(question.get("question") or "")
    if raw_question and "matched policy policy_" not in raw_question:
        return raw_question
    action_id = str(question.get("action_id") or "").strip()
    if not action_id:
        return raw_question or None
    try:
        action = get_action_for_queue(context, action_id)
    except ValueError:
        return raw_question or None
    policy_id = str(action.get("policy_id") or "").strip()
    if not policy_id:
        return raw_question or None
    return human_policy_reason(
        str(action.get("action_type") or "action"),
        {
            "id": policy_id,
            "autonomy_level": action.get("autonomy_level") or "L3",
        },
        action.get("action_features") or {},
    )


def question_candidate_fact(
    context: BrainPaths | QueueBuildContext, question: dict[str, Any]
) -> dict[str, Any] | None:
    for option in question.get("options") or []:
        if isinstance(option, dict) and option.get("option_type") == "candidate_fact":
            return enrich_fact_like(context, option)
    recommended = question.get("recommended_action") or {}
    payload = recommended.get("payload") if isinstance(recommended, dict) else {}
    if isinstance(payload, dict) and isinstance(payload.get("fact"), dict):
        return enrich_fact_like(context, payload["fact"])
    action_id = str(question.get("action_id") or "").strip()
    if action_id:
        try:
            action = get_action_for_queue(context, action_id)
        except ValueError:
            return None
        payload = (action.get("evidence_json") or {}).get("payload") or {}
        if isinstance(payload, dict) and isinstance(payload.get("fact"), dict):
            return enrich_fact_like(context, payload["fact"])
    return None


def question_counterpart_facts(
    context: BrainPaths | QueueBuildContext, question: dict[str, Any]
) -> list[dict[str, Any]]:
    counterparts: list[dict[str, Any]] = []
    for option in question.get("options") or []:
        if isinstance(option, dict) and option.get("option_type") == "existing_fact":
            counterparts.append(enrich_fact_like(context, option))
    question_context = question.get("context") or {}
    fact_ids = [
        str(fact_id)
        for fact_id in question_context.get("counterpart_fact_ids") or []
        if str(fact_id or "").strip()
    ]
    if not fact_ids and question.get("kind") == "conflict":
        fact_ids = [str(fact_id) for fact_id in question.get("fact_ids") or [] if fact_id]
    if fact_ids:
        known = {str(item.get("id") or item.get("fact_id") or "") for item in counterparts}
        for fact in facts_by_id(context, fact_ids).values():
            if fact["id"] not in known:
                counterparts.append(enrich_fact_like(context, fact))
    return counterparts


def queue_item_for_action(
    context: BrainPaths | QueueBuildContext, action: dict[str, Any]
) -> dict[str, Any]:
    payload = (action.get("evidence_json") or {}).get("payload")
    title = action_title(action, payload)
    return {
        "id": action["id"],
        "source_type": "action",
        "kind": "proposed_action",
        "group": "topology",
        "title": title,
        "summary": action_summary(action, payload),
        "created_at": action.get("created_at"),
        "status": action.get("status"),
        "risk_tier": action.get("risk_tier"),
        "action": action,
        "proposal": payload,
        "raw": action,
    }


def queue_item_for_audit_action(
    context: BrainPaths | QueueBuildContext, action: dict[str, Any]
) -> dict[str, Any]:
    return {
        "id": action["id"],
        "source_type": "audit",
        "kind": "audit_flagged",
        "group": "audit",
        "title": f"Audit flagged {action.get('action_type')}",
        "summary": compact_text(
            (action.get("evidence_json") or {}).get("audit", {}).get("rationale")
            or (action.get("evidence_json") or {}).get("reason")
            or action.get("audit_status"),
            220,
        ),
        "created_at": action.get("applied_at") or action.get("created_at"),
        "status": action.get("status"),
        "risk_tier": action.get("risk_tier"),
        "action": action,
        "raw": action,
    }


def queue_item_for_memory(
    context: BrainPaths | QueueBuildContext, memory: dict[str, Any]
) -> dict[str, Any]:
    enriched = enrich_memory_detail_for_queue(context, memory)
    return {
        "id": memory["id"],
        "source_type": "memory",
        "kind": "proposed_memory",
        "group": "memories",
        "title": compact_text(memory.get("content"), 120),
        "summary": f"{memory.get('memory_type')} · {memory.get('scope')}",
        "created_at": memory.get("updated_at") or memory.get("created_at"),
        "status": memory.get("status"),
        "risk_tier": "low",
        "memory": enriched,
        "raw": enriched,
    }


def action_title(action: dict[str, Any], payload: Any) -> str:
    action_type = str(action.get("action_type") or "action")
    if isinstance(payload, dict):
        if action_type == "entity_merge":
            names = payload.get("entity_names") or {}
            ids = [payload.get("canonical_entity_id"), *(payload.get("merged_entity_ids") or [])]
            surfaces = [str(names.get(entity_id) or entity_id) for entity_id in ids if entity_id]
            return f"Merge entities: {', '.join(surfaces)}"
        if payload.get("candidate") and isinstance(payload["candidate"], dict):
            return action_title(action, payload["candidate"])
        for key in ("page_hint", "destination_page_hint", "target_path"):
            if payload.get(key):
                return f"{action_type}: {payload[key]}"
    return f"{action_type} · {short_id(str(action.get('id') or ''))}"


def action_summary(action: dict[str, Any], payload: Any) -> str:
    if isinstance(payload, dict):
        return compact_text(
            payload.get("reason")
            or payload.get("candidate_signal")
            or json.dumps(payload, sort_keys=True),
            240,
        )
    return compact_text((action.get("evidence_json") or {}).get("reason"), 240)


def enrich_fact_like(
    context: BrainPaths | QueueBuildContext, fact: dict[str, Any]
) -> dict[str, Any]:
    output = dict(fact)
    if "id" not in output and output.get("fact_id"):
        output["id"] = output["fact_id"]
    source_ids = output.get("source_ids") or []
    if isinstance(source_ids, str):
        source_ids = json_loads(source_ids, [])
    output["source_ids"] = [str(source_id) for source_id in source_ids if source_id]
    output["source_documents"] = queue_source_document_summaries(
        context, output["source_ids"]
    )
    return output


def enrich_memory_detail_for_queue(
    context: BrainPaths | QueueBuildContext, memory: dict[str, Any]
) -> dict[str, Any]:
    output = dict(memory)
    output["source_documents"] = queue_source_document_summaries(
        context, output.get("source_ids") or []
    )
    output["audit"] = {"errors": [], "warnings": []}
    return output


def queue_source_document_summaries(
    context: BrainPaths | QueueBuildContext, source_ids: list[str]
) -> list[dict[str, Any]]:
    if isinstance(context, QueueBuildContext):
        return context.source_documents(source_ids)
    return source_document_summaries(context, source_ids)


def facts_by_id(
    context: BrainPaths | QueueBuildContext, fact_ids: list[str]
) -> dict[str, dict[str, Any]]:
    if not fact_ids:
        return {}
    if isinstance(context, QueueBuildContext):
        return context.facts(fact_ids)
    placeholders = ",".join("?" for _ in fact_ids)
    paths = queue_context_paths(context)
    with connection(paths.sqlite_path) as conn:
        if not ui_table_exists(conn, "facts"):
            return {}
        rows = conn.execute(
            f"SELECT * FROM facts WHERE id IN ({placeholders})",
            fact_ids,
        )
        return {str(row["id"]): row_to_fact(row) for row in rows}


def route_candidates_for_fact(
    context: BrainPaths | QueueBuildContext, fact: dict[str, Any]
) -> list[dict[str, Any]]:
    statement = str(fact.get("statement") or "")
    page_hint = str(fact.get("page_hint") or "")
    query = " ".join(
        item
        for item in [
            page_hint.replace("/", " ").replace(".md", ""),
            str(fact.get("entity_key") or "").replace(":", " "),
            statement,
        ]
        if item
    )
    pages = (
        context.active_pages()
        if isinstance(context, QueueBuildContext)
        else wiki_pages_from_index(context, page_type=None, status="active")
    )
    terms = {term.casefold() for term in query.replace("-", " ").split() if len(term) > 2}
    scored: list[tuple[int, dict[str, Any]]] = []
    for page in pages:
        haystack = " ".join(
            [str(page.get("title") or ""), str(page.get("relative_path") or "")]
        ).casefold()
        score = sum(1 for term in terms if term in haystack)
        if page.get("relative_path") == page_hint:
            score += 10
        if score:
            scored.append((score, page))
    scored.sort(key=lambda item: (-item[0], item[1].get("relative_path") or ""))
    return [
        {
            "page_hint": page["relative_path"],
            "title": page["title"],
            "score": score,
            "page_type": page.get("page_type"),
        }
        for score, page in scored[:5]
    ]


def queue_active_pages(paths: BrainPaths, conn: Any) -> list[dict[str, Any]]:
    if not ui_table_exists(conn, "wiki_pages"):
        return []
    pages = []
    rows = conn.execute(
        """
        SELECT title, page_type, path
        FROM wiki_pages
        WHERE status = 'active'
        ORDER BY page_type, title
        """
    )
    wiki_root = paths.wiki.resolve()
    for row in rows:
        try:
            target = Path(str(row["path"]))
            relative_path = target.resolve().relative_to(wiki_root).as_posix()
            safe_wiki_path(paths, relative_path, must_exist=False)
        except (ValueError, BadRequestError, NotFoundError, OSError):
            continue
        pages.append(
            {
                "title": row["title"],
                "relative_path": relative_path,
                "page_type": row["page_type"],
            }
        )
    return pages


def get_action_for_queue(
    context: BrainPaths | QueueBuildContext, action_id: str
) -> dict[str, Any]:
    if isinstance(context, QueueBuildContext):
        return context.action(action_id)
    paths = queue_context_paths(context)
    with connection(paths.sqlite_path) as conn:
        return load_action(conn, action_id)


def compact_text(value: Any, limit: int = 160) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return f"{text[: max(0, limit - 1)].rstrip()}..."


def short_id(value: str) -> str:
    return value[:10]


def bounded_int(
    value: str | None, *, default: int, minimum: int, maximum: int
) -> int:
    if value is None:
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, parsed))


def ui_queue_decision(
    paths: BrainPaths, item_id: str, payload: dict[str, Any]
) -> dict[str, Any]:
    service(paths).init_workspace()
    decision = str(payload.get("decision") or "").strip()
    if not decision:
        raise BadRequestError("decision is required")
    existing = find_queue_target(paths, item_id)
    source_type = existing["source_type"]
    if source_type == "question":
        return decide_queue_question(paths, existing["item"], decision, payload)
    if source_type == "memory":
        return decide_queue_memory(paths, existing["item"], decision, payload)
    if source_type == "action":
        return decide_queue_action(paths, existing["item"], decision, payload)
    if source_type == "audit":
        return decide_queue_audit(paths, existing["item"], decision, payload)
    raise NotFoundError(f"queue item not found: {item_id}")


def find_queue_target(paths: BrainPaths, item_id: str) -> dict[str, Any]:
    with connection(paths.sqlite_path) as conn:
        if ui_table_exists(conn, "open_questions"):
            row = conn.execute(
                "SELECT * FROM open_questions WHERE id = ?", (item_id,)
            ).fetchone()
            if row:
                return {"source_type": "question", "item": row_to_question(row)}
        if ui_table_exists(conn, "memories"):
            row = conn.execute("SELECT * FROM memories WHERE id = ?", (item_id,)).fetchone()
            if row:
                return {"source_type": "memory", "item": service(paths).get_memory(item_id)}
        if ui_table_exists(conn, "cos_actions"):
            row = conn.execute(
                "SELECT * FROM cos_actions WHERE id = ?", (item_id,)
            ).fetchone()
            if row:
                action = row_to_action(row)
                if action.get("audit_status") == "sampled_bad":
                    return {"source_type": "audit", "item": action}
                return {"source_type": "action", "item": action}
    raise NotFoundError(f"queue item not found: {item_id}")


def decide_queue_question(
    paths: BrainPaths,
    question: dict[str, Any],
    decision: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    normalized = decision.replace("-", "_")
    if normalized in {"skip", "escalate"}:
        return {
            "status": "skipped",
            "item_id": question["id"],
            "result": {"question": question},
            "undo_handle": None,
        }
    previous = question_undo_state(question)
    if question.get("kind") == "unrouted_fact" and normalized in {"route", "new_page"}:
        return route_unrouted_question(paths, question, payload, previous)
    if normalized in {"both_true", "contested"}:
        return contest_question_candidate(paths, question, previous)
    if normalized in {"apply", "approve", "candidate", "candidate_wins", "accept"}:
        action_states = linked_question_action_states(paths, question)
        result = apply_question_candidate(paths, question, payload)
        action_ids = action_ids_from_result(result)
        return {
            "status": "decided",
            "item_id": question["id"],
            "result": result,
            "undo_handle": {
                "kind": "question_actions",
                "question": previous,
                "action_ids": action_ids,
                "actions": action_states,
            },
        }
    if normalized in {"reject", "reject_candidate", "keep_existing", "dismiss"}:
        result = reject_question_candidate(paths, question, payload)
        return {
            "status": "decided",
            "item_id": question["id"],
            "result": result,
            "undo_handle": {
                "kind": "question_reject",
                "question": previous,
                "action": action_undo_state(paths, str(question.get("action_id") or "")),
            },
        }
    if normalized in {"select_fact", "keep_fact"}:
        selected_fact_id = str(payload.get("selected_fact_id") or "").strip()
        if not selected_fact_id:
            raise BadRequestError("selected_fact_id is required")
        action_states = linked_question_action_states(paths, question)
        result = ui_answer_wiki_question(
            paths, question["id"], {"selected_fact_id": selected_fact_id}
        )
        return {
            "status": "decided",
            "item_id": question["id"],
            "result": result,
            "undo_handle": {
                "kind": "question_actions",
                "question": previous,
                "action_ids": action_ids_from_result(result),
                "actions": action_states,
            },
        }
    raise BadRequestError(f"unsupported queue decision: {decision}")


def apply_question_candidate(
    paths: BrainPaths, question: dict[str, Any], payload: dict[str, Any]
) -> dict[str, Any]:
    action_id = str(question.get("action_id") or "").strip()
    if action_id:
        return ui_apply_cos_question_action(paths, question["id"], payload)
    selected_fact_id = str(payload.get("selected_fact_id") or "").strip()
    if selected_fact_id:
        return ui_answer_wiki_question(
            paths, question["id"], {"selected_fact_id": selected_fact_id}
        )
    candidate = question_candidate_fact(paths, question)
    if candidate and candidate.get("id"):
        return ui_answer_wiki_question(
            paths, question["id"], {"selected_fact_id": str(candidate["id"])}
        )
    raise BadRequestError(f"review question has no applicable action: {question['id']}")


def reject_question_candidate(
    paths: BrainPaths, question: dict[str, Any], payload: dict[str, Any]
) -> dict[str, Any]:
    action_id = str(question.get("action_id") or "").strip()
    if action_id:
        return ui_dismiss_cos_question(
            paths,
            question["id"],
            {"reason": payload.get("reason") or "human rejected queue item"},
        )
    counterpart = first_counterpart_fact_id(paths, question)
    if counterpart:
        return ui_answer_wiki_question(
            paths, question["id"], {"selected_fact_id": counterpart}
        )
    mark_review_question_decided(
        paths,
        question["id"],
        status="dismissed",
        answer={"decision": "dismiss", "reason": payload.get("reason") or ""},
        action_id=None,
    )
    return {"question": get_review_question(paths, question["id"])}


def route_unrouted_question(
    paths: BrainPaths,
    question: dict[str, Any],
    payload: dict[str, Any],
    previous: dict[str, Any],
) -> dict[str, Any]:
    page_hint = str(payload.get("page_hint") or "").strip()
    if not page_hint:
        raise BadRequestError("page_hint is required")
    safe_wiki_path(paths, page_hint, must_exist=False)
    candidate = question_candidate_fact(paths, question)
    if not candidate:
        raise BadRequestError("unrouted question has no fact candidate")
    old_action_id = str(question.get("action_id") or "").strip()
    old_action = action_undo_state(paths, old_action_id)
    routed = dict(candidate)
    routed["page_hint"] = page_hint
    routed.setdefault("section_hint", "Summary")
    if not isinstance(routed.get("metadata"), dict):
        routed["metadata"] = {}
    routed["metadata"] = {
        **routed["metadata"],
        "ui_route_decision": {
            "question_id": question["id"],
            "old_page_hint": candidate.get("page_hint"),
            "new_page_hint": page_hint,
            "decided_at": now_iso(),
        },
    }
    action = apply_action(
        paths,
        propose_action(
            paths,
            "fact_upsert",
            action_payload={"fact": routed},
            action_features={
                "human_confirmed": True,
                "truth_mutation": True,
                "reversible": True,
                "affected_fact_count": 1,
            },
            target_fact_ids=[str(routed.get("id") or "")],
            target_page_paths=[page_hint],
            proposed_by="ui_queue_route",
            confidence=float(routed.get("confidence") or 1.0),
            risk_tier="medium",
        )["id"],
    )
    if old_action_id:
        reject_linked_review_action(
            paths, old_action_id, "replaced by explicit UI route decision"
        )
    mark_review_question_decided(
        paths,
        question["id"],
        status="answered",
        answer={
            "decision": "route",
            "page_hint": page_hint,
            "new_action_id": action["id"],
            "old_action_id": old_action_id,
        },
        action_id=action["id"],
    )
    result = {"question": get_review_question(paths, question["id"]), "action": action}
    return {
        "status": "decided",
        "item_id": question["id"],
        "result": result,
        "undo_handle": {
            "kind": "question_route",
            "question": previous,
            "new_action_id": action["id"],
            "old_action": old_action,
        },
    }


def contest_question_candidate(
    paths: BrainPaths, question: dict[str, Any], previous: dict[str, Any]
) -> dict[str, Any]:
    action_id = str(question.get("action_id") or "").strip()
    action_states = linked_question_action_states(paths, question)
    applied_ids: list[str] = []
    candidate_fact_ids: list[str] = []
    if action_id:
        applied = apply_action(paths, action_id)
        applied_ids.append(applied["id"])
        candidate_fact_ids.extend(str(item) for item in applied.get("target_fact_ids") or [])
    candidate = question_candidate_fact(paths, question)
    if candidate and candidate.get("id"):
        candidate_fact_ids.append(str(candidate["id"]))
    counterpart_ids = [
        str(fact.get("id") or fact.get("fact_id"))
        for fact in question_counterpart_facts(paths, question)
        if fact.get("id") or fact.get("fact_id")
    ]
    fact_ids = stable_unique_strings([*candidate_fact_ids, *counterpart_ids])
    if len(fact_ids) < 2:
        raise BadRequestError("both_true requires at least two fact ids")
    contested = apply_action(
        paths,
        propose_action(
            paths,
            "display_contested",
            action_payload={"fact_ids": fact_ids, "question_id": question["id"]},
            action_features={
                "human_confirmed": True,
                "truth_mutation": True,
                "reversible": True,
                "affected_fact_count": len(fact_ids),
            },
            target_fact_ids=fact_ids,
            target_page_paths=[str(question.get("page_hint"))]
            if question.get("page_hint")
            else [],
            proposed_by="ui_queue_contested",
            confidence=1.0,
            risk_tier="medium",
        )["id"],
    )
    applied_ids.append(contested["id"])
    mark_review_question_decided(
        paths,
        question["id"],
        status="answered",
        answer={"decision": "both_true", "fact_ids": fact_ids},
        action_id=contested["id"],
    )
    result = {
        "question": get_review_question(paths, question["id"]),
        "actions": [get_action_for_queue(paths, action_id) for action_id in applied_ids],
    }
    return {
        "status": "decided",
        "item_id": question["id"],
        "result": result,
        "undo_handle": {
            "kind": "question_actions",
            "question": previous,
            "action_ids": list(reversed(applied_ids)),
            "actions": action_states,
        },
    }


def first_counterpart_fact_id(paths: BrainPaths, question: dict[str, Any]) -> str | None:
    for fact in question_counterpart_facts(paths, question):
        fact_id = str(fact.get("id") or fact.get("fact_id") or "").strip()
        if fact_id:
            return fact_id
    return None


def decide_queue_memory(
    paths: BrainPaths,
    memory: dict[str, Any],
    decision: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    normalized = decision.replace("-", "_")
    previous = {
        "id": memory["id"],
        "status": memory.get("status"),
        "reviewed_at": memory.get("reviewed_at"),
        "review_reason": memory.get("review_reason"),
    }
    svc = service(paths)
    if normalized in {"approve", "accept", "apply"}:
        result = enrich_memory_detail(paths, svc.approve_memory(memory["id"]))
    elif normalized == "reject":
        result = enrich_memory_detail(
            paths,
            svc.reject_memory(
                memory["id"], str(payload.get("reason") or "human rejected memory")
            ),
        )
    elif normalized in {"archive", "dismiss"}:
        result = enrich_memory_detail(paths, svc.archive_memory(memory["id"]))
    else:
        raise BadRequestError(f"unsupported memory decision: {decision}")
    return {
        "status": "decided",
        "item_id": memory["id"],
        "result": {"memory": result},
        "undo_handle": {"kind": "memory_status", "memory": previous},
    }


def decide_queue_action(
    paths: BrainPaths,
    action: dict[str, Any],
    decision: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    normalized = decision.replace("-", "_")
    previous = action_undo_state(paths, action["id"])
    if normalized in {"approve", "apply", "accept"}:
        result = apply_action(paths, action["id"])
        undo = {"kind": "action_apply", "action_id": action["id"]}
    elif normalized in {"reject", "dismiss"}:
        result = reject_linked_review_action(
            paths,
            action["id"],
            str(payload.get("reason") or "human rejected queue action"),
        )
        undo = {"kind": "action_status", "action": previous}
    else:
        raise BadRequestError(f"unsupported action decision: {decision}")
    return {
        "status": "decided",
        "item_id": action["id"],
        "result": {"action": result},
        "undo_handle": undo,
    }


def decide_queue_audit(
    paths: BrainPaths,
    action: dict[str, Any],
    decision: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    normalized = decision.replace("-", "_")
    previous = action_undo_state(paths, action["id"])
    if normalized in {"revert", "v"}:
        result = revert_action(paths, action["id"])
        undo = {"kind": "action_revert", "action": previous}
    elif normalized in {"ok", "mark_ok", "mark_good"}:
        result = record_action_audit(
            paths,
            action["id"],
            "sampled_ok",
            metadata={"ui_marked_ok": True, "note": payload.get("note") or ""},
        )
        undo = {"kind": "action_status", "action": previous}
    else:
        raise BadRequestError(f"unsupported audit decision: {decision}")
    return {
        "status": "decided",
        "item_id": action["id"],
        "result": {"action": result},
        "undo_handle": undo,
    }


def ui_queue_undo(paths: BrainPaths, payload: dict[str, Any]) -> dict[str, Any]:
    handle = payload.get("undo_handle") or payload
    if not isinstance(handle, dict):
        raise BadRequestError("undo_handle is required")
    kind = str(handle.get("kind") or "").strip()
    if kind == "question_actions":
        action_states = {
            str(state.get("id")): state
            for state in handle.get("actions") or []
            if isinstance(state, dict) and str(state.get("id") or "").strip()
        }
        for action_id in handle.get("action_ids") or []:
            action_id = str(action_id)
            revert_action(paths, action_id)
            restore_action_state(paths, action_states.get(action_id))
        restore_question_state(paths, handle.get("question") or {})
    elif kind == "question_reject":
        restore_action_state(paths, handle.get("action") or {})
        restore_question_state(paths, handle.get("question") or {})
    elif kind == "question_route":
        if handle.get("new_action_id"):
            revert_action(paths, str(handle["new_action_id"]))
        restore_action_state(paths, handle.get("old_action") or {})
        restore_question_state(paths, handle.get("question") or {})
    elif kind == "memory_status":
        restore_memory_state(paths, handle.get("memory") or {})
    elif kind == "action_apply":
        revert_action(paths, str(handle.get("action_id") or ""))
    elif kind == "action_revert":
        action_state = handle.get("action") or {}
        action_id = str(action_state.get("id") or "").strip()
        if action_id:
            apply_action(paths, action_id)
        restore_action_state(paths, action_state)
    elif kind == "action_status":
        restore_action_state(paths, handle.get("action") or {})
    else:
        raise BadRequestError(f"unsupported undo handle: {kind}")
    return {"status": "undone", "undo_handle": handle}


def question_undo_state(question: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": question["id"],
        "status": question.get("status"),
        "answer": question.get("answer"),
        "answered_at": question.get("answered_at"),
        "action_id": question.get("action_id"),
        "decided_by": question.get("decided_by"),
    }


def action_undo_state(paths: BrainPaths, action_id: str) -> dict[str, Any] | None:
    if not action_id:
        return None
    try:
        action = get_action_for_queue(paths, action_id)
    except ValueError:
        return None
    return {
        "id": action["id"],
        "status": action.get("status"),
        "audit_status": action.get("audit_status"),
        "target_fact_ids": action.get("target_fact_ids"),
        "target_page_paths": action.get("target_page_paths"),
        "target_contract_ids": action.get("target_contract_ids"),
        "evidence_json": action.get("evidence_json"),
        "policy_decision": action.get("policy_decision"),
        "autonomy_level": action.get("autonomy_level"),
        "inverse_action_json": action.get("inverse_action_json"),
        "applied_state_hash": action.get("applied_state_hash"),
        "applied_at": action.get("applied_at"),
    }


def linked_question_action_states(
    paths: BrainPaths, question: dict[str, Any]
) -> list[dict[str, Any]]:
    action_id = str(question.get("action_id") or "").strip()
    state = action_undo_state(paths, action_id)
    return [state] if state else []


def action_ids_from_result(result: dict[str, Any]) -> list[str]:
    action_ids: list[str] = []
    if isinstance(result.get("action"), dict) and result["action"].get("id"):
        action_ids.append(str(result["action"]["id"]))
    for action in result.get("actions") or []:
        if isinstance(action, dict) and action.get("id"):
            action_ids.append(str(action["id"]))
    return action_ids


def restore_question_state(paths: BrainPaths, state: dict[str, Any]) -> None:
    question_id = str(state.get("id") or "").strip()
    if not question_id:
        return
    with connection(paths.sqlite_path) as conn:
        conn.execute(
            """
            UPDATE open_questions
            SET status = ?, answer = ?, answered_at = ?, action_id = ?, decided_by = ?
            WHERE id = ?
            """,
            (
                state.get("status"),
                dumps(state.get("answer")) if state.get("answer") is not None else None,
                state.get("answered_at"),
                state.get("action_id"),
                state.get("decided_by"),
                question_id,
            ),
        )


def restore_action_state(paths: BrainPaths, state: dict[str, Any] | None) -> None:
    if not state:
        return
    action_id = str(state.get("id") or "").strip()
    if not action_id:
        return
    with connection(paths.sqlite_path) as conn:
        conn.execute(
            """
            UPDATE cos_actions
            SET status = ?, audit_status = ?, target_fact_ids = ?,
                target_page_paths = ?, target_contract_ids = ?, evidence_json = ?,
                policy_decision = ?, autonomy_level = ?, inverse_action_json = ?,
                applied_state_hash = ?, applied_at = ?
            WHERE id = ?
            """,
            (
                state.get("status"),
                state.get("audit_status"),
                dumps(state.get("target_fact_ids") or []),
                dumps(state.get("target_page_paths") or []),
                dumps(state.get("target_contract_ids") or []),
                dumps(state.get("evidence_json") or {}),
                state.get("policy_decision"),
                state.get("autonomy_level"),
                dumps(state.get("inverse_action_json"))
                if state.get("inverse_action_json") is not None
                else None,
                state.get("applied_state_hash"),
                state.get("applied_at"),
                action_id,
            ),
        )


def restore_memory_state(paths: BrainPaths, state: dict[str, Any]) -> None:
    memory_id = str(state.get("id") or "").strip()
    if not memory_id:
        return
    with connection(paths.sqlite_path) as conn:
        conn.execute(
            """
            UPDATE memories
            SET status = ?, reviewed_at = ?, review_reason = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                state.get("status"),
                state.get("reviewed_at"),
                state.get("review_reason"),
                now_iso(),
                memory_id,
            ),
        )
    service(paths).export_all_memories()


def stable_unique_strings(values: list[Any]) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if text and text not in seen:
            output.append(text)
            seen.add(text)
    return output


def ui_wiki_pages(paths: BrainPaths, query: dict[str, list[str]]) -> dict[str, Any]:
    svc = service(paths)
    svc.init_workspace()
    lint_wiki(paths)
    page_type = first(query, "type")
    status = first(query, "status")
    if page_type and page_type not in ALLOWED_PAGE_TYPES:
        raise BadRequestError(f"invalid wiki page type: {page_type}")
    if status and status not in ALLOWED_STATUSES:
        raise BadRequestError(f"invalid wiki page status: {status}")
    q = first(query, "q")
    if q:
        pages = []
        for result in svc.search_wiki_pages(q, limit=50):
            relative_path = str(result.get("relative_path") or "")
            try:
                target = safe_wiki_path(paths, relative_path)
                page = wiki_page_entry(paths, target)
            except (BadRequestError, NotFoundError):
                continue
            if page_type and page["page_type"] != page_type:
                continue
            if status and page["status"] != status:
                continue
            page["score"] = result.get("score")
            page["summary"] = result.get("summary") or ""
            pages.append(page)
    else:
        pages = wiki_pages_from_index(paths, page_type=page_type, status=status)
    return {"pages": pages, "count": len(pages)}


def wiki_pages_from_index(
    paths: BrainPaths,
    *,
    page_type: str | None,
    status: str | None,
) -> list[dict[str, Any]]:
    query = "SELECT * FROM wiki_pages WHERE 1=1"
    params: list[Any] = []
    if page_type:
        query += " AND page_type = ?"
        params.append(page_type)
    if status:
        query += " AND status = ?"
        params.append(status)
    query += " ORDER BY page_type, title"
    pages: list[dict[str, Any]] = []
    with connection(paths.sqlite_path) as conn:
        page_rows = list(conn.execute(query, params))
    for row in page_rows:
        target = Path(str(row["path"]))
        try:
            relative_path = (
                target.resolve().relative_to(paths.wiki.resolve()).as_posix()
            )
            safe_target = safe_wiki_path(paths, relative_path)
        except (ValueError, BadRequestError, NotFoundError):
            continue
        page = wiki_page_entry(paths, safe_target, indexed_row=dict(row))
        pages.append(page)
    return pages


def wiki_page_entry(
    paths: BrainPaths,
    target: Path,
    indexed_row: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not target.exists():
        raise NotFoundError("wiki page not found")
    text = target.read_text(encoding="utf-8", errors="replace")
    frontmatter, _body = parse_frontmatter(text)
    if frontmatter is None:
        if indexed_row is None:
            raise BadRequestError("wiki page has invalid frontmatter")
        frontmatter = {}
    relative_path = target.relative_to(paths.wiki).as_posix()
    source_ids = list(
        frontmatter.get("source_ids")
        or parse_stored_list((indexed_row or {}).get("source_ids"))
    )
    return {
        "title": str(
            frontmatter.get("title") or (indexed_row or {}).get("title") or target.stem
        ),
        "page_type": str(
            frontmatter.get("page_type") or (indexed_row or {}).get("page_type") or ""
        ),
        "status": str(
            frontmatter.get("status") or (indexed_row or {}).get("status") or ""
        ),
        "relative_path": relative_path,
        "source_ids": source_ids,
        "source_count": len(source_ids),
        "updated_at": str(
            frontmatter.get("updated_at") or (indexed_row or {}).get("updated_at") or ""
        ),
        "generated": GENERATED_MARKER in text,
        "related": list(
            frontmatter.get("related")
            or parse_stored_list((indexed_row or {}).get("related"))
        ),
    }


def ui_wiki_page(paths: BrainPaths, query: dict[str, list[str]]) -> dict[str, Any]:
    service(paths).init_workspace()
    relative_path = first(query, "path")
    target = safe_wiki_path(paths, relative_path or "")
    if not target.exists():
        raise NotFoundError(f"wiki page not found: {relative_path}")
    text = target.read_text(encoding="utf-8", errors="replace")
    frontmatter, body = parse_frontmatter(text)
    if frontmatter is None:
        raise BadRequestError(f"wiki page has invalid frontmatter: {relative_path}")
    source_ids = list(frontmatter.get("source_ids") or [])
    return {
        "relative_path": target.relative_to(paths.wiki).as_posix(),
        "frontmatter": frontmatter,
        "body": body,
        "markdown": text,
        "generated": GENERATED_MARKER in text,
        "source_ids": source_ids,
        "source_documents": source_document_summaries(paths, source_ids),
        "facts": facts_for_page(paths, target.relative_to(paths.wiki).as_posix()),
        "contract": contract_for_page(paths, target.relative_to(paths.wiki).as_posix()),
        "snapshots": snapshots_for_page(paths, target.relative_to(paths.wiki).as_posix()),
        "related": list(frontmatter.get("related") or []),
    }


def facts_for_page(paths: BrainPaths, relative_path: str) -> list[dict[str, Any]]:
    with connection(paths.sqlite_path) as conn:
        if not ui_table_exists(conn, "facts"):
            return []
        rows = conn.execute(
            """
            SELECT *
            FROM facts
            WHERE page_hint = ?
              AND status IN ('active', 'conflicted')
            ORDER BY section_hint, observed_at DESC, created_at DESC
            LIMIT 500
            """,
            (relative_path,),
        )
        facts = [row_to_fact(row) for row in rows]
    return [enrich_fact_like(paths, fact) for fact in facts]


def contract_for_page(paths: BrainPaths, relative_path: str) -> dict[str, Any] | None:
    for contract in active_page_contracts(paths):
        if str(contract.get("page_hint") or "") == relative_path:
            return contract
    return None


def snapshots_for_page(paths: BrainPaths, relative_path: str) -> list[dict[str, Any]]:
    with connection(paths.sqlite_path) as conn:
        if not ui_table_exists(conn, "wiki_page_snapshots"):
            return []
        rows = conn.execute(
            """
            SELECT id, page_path, before_exists, after_exists, reason, metadata,
                   created_at, before_markdown, after_markdown
            FROM wiki_page_snapshots
            WHERE page_path = ?
            ORDER BY created_at DESC
            LIMIT 20
            """,
            (relative_path,),
        )
        snapshots = []
        for row in rows:
            snapshot = row_to_plain_dict(row)
            before = str(snapshot.get("before_markdown") or "")
            after = str(snapshot.get("after_markdown") or "")
            snapshot["before_preview"] = compact_text(before, 180)
            snapshot["after_preview"] = compact_text(after, 180)
            snapshots.append(snapshot)
        return snapshots


def ui_save_wiki_page(paths: BrainPaths, payload: dict[str, Any]) -> dict[str, Any]:
    service(paths).init_workspace()
    relative_path = str(payload.get("path") or payload.get("relative_path") or "").strip()
    markdown = str(payload.get("markdown") or "")
    target = safe_wiki_path(paths, relative_path, must_exist=True)
    existing = target.read_text(encoding="utf-8", errors="replace")
    if GENERATED_MARKER in existing:
        raise BadRequestError("managed pages are projections; edit facts instead")
    frontmatter, _body = parse_frontmatter(markdown)
    if frontmatter is None:
        raise BadRequestError("markdown must include valid frontmatter")
    target.write_text(markdown, encoding="utf-8")
    return ui_wiki_page(paths, {"path": [relative_path]})


def ui_confirm_fact(paths: BrainPaths, fact_id: str) -> dict[str, Any]:
    service(paths).init_workspace()
    facts = facts_by_id(paths, [fact_id])
    fact = facts.get(fact_id)
    if not fact:
        raise NotFoundError(f"fact not found: {fact_id}")
    confirmed = {**fact, "confirmed_by_user": True}
    action = apply_action(
        paths,
        propose_action(
            paths,
            "fact_upsert",
            action_payload={"fact": confirmed},
            action_features={
                "human_confirmed": True,
                "truth_mutation": True,
                "reversible": True,
                "affected_fact_count": 1,
            },
            target_fact_ids=[fact_id],
            target_page_paths=[str(fact.get("page_hint"))]
            if fact.get("page_hint")
            else [],
            proposed_by="ui_fact_confirm",
            confidence=1.0,
            risk_tier="low",
        )["id"],
    )
    return {"fact": facts_by_id(paths, [fact_id]).get(fact_id), "action": action}


def ui_flag_fact(
    paths: BrainPaths, fact_id: str, payload: dict[str, Any]
) -> dict[str, Any]:
    service(paths).init_workspace()
    facts = facts_by_id(paths, [fact_id])
    fact = facts.get(fact_id)
    if not fact:
        raise NotFoundError(f"fact not found: {fact_id}")
    question_id = new_id("question")
    reason = str(payload.get("reason") or "Human flagged fact from UI").strip()
    with connection(paths.sqlite_path) as conn:
        conn.execute(
            """
            INSERT INTO open_questions(
              id, kind, entity_key, page_hint, fact_ids, question, options,
              status, context, risk_tier, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                question_id,
                "fact_flag",
                fact.get("entity_key"),
                fact.get("page_hint"),
                dumps([fact_id]),
                reason,
                dumps([fact]),
                "needs_human",
                dumps({"flagged_fact_id": fact_id, "ui_payload": payload}),
                "medium",
                now_iso(),
            ),
        )
    return {"question": get_review_question(paths, question_id)}


def ui_retrieve(paths: BrainPaths, payload: dict[str, Any]) -> dict[str, Any]:
    task = str(payload.get("task") or payload.get("query") or "").strip()
    if not task:
        raise BadRequestError("task is required")
    mode = str(payload.get("mode") or "default").strip() or "default"
    budget = payload.get("budget")
    return service(paths).retrieve_context(
        task=task,
        project=optional_str(payload.get("project")),
        budget=int(budget) if budget is not None else None,
        mode=mode,
        debug=bool(payload.get("debug", False)),
    )


def ui_search(paths: BrainPaths, query: dict[str, list[str]]) -> dict[str, Any]:
    q = first(query, "q")
    if not q:
        raise BadRequestError("q is required")
    limit = bounded_int(first(query, "limit"), default=10, minimum=1, maximum=50)
    debug = first(query, "debug") in {"1", "true", "yes"}
    return service(paths).search(q, limit=limit, debug=debug, caller="ui")


def ui_entities(paths: BrainPaths, query: dict[str, list[str]]) -> dict[str, Any]:
    service(paths).init_workspace()
    entity_type = first(query, "type")
    q = (first(query, "q") or "").casefold()
    show_inactive = first(query, "inactive") in {"1", "true", "yes"}
    with connection(paths.sqlite_path) as conn:
        if not ui_table_exists(conn, "entities"):
            return {"entities": [], "count": 0, "types": []}
        where = "WHERE 1=1"
        params: list[Any] = []
        if entity_type:
            where += " AND entity_type = ?"
            params.append(entity_type)
        if not show_inactive:
            where += " AND COALESCE(e.status, 'active') = 'active'"
        rows = [
            row_to_plain_dict(row)
            for row in conn.execute(
                f"""
                SELECT e.*,
                       COUNT(DISTINCT f.id) AS fact_count,
                       MAX(COALESCE(f.observed_at, f.created_at)) AS last_observed_at
                FROM entities e
                LEFT JOIN fact_entities fe ON fe.entity_id = e.id
                LEFT JOIN facts f ON f.id = fe.fact_id
                  AND f.status IN ('active', 'conflicted')
                {where}
                GROUP BY e.id
                ORDER BY fact_count DESC, e.name
                LIMIT 1000
                """,
                params,
            )
        ]
        type_rows = conn.execute(
            """
            SELECT COALESCE(entity_type, 'other') AS entity_type, COUNT(*) AS count
            FROM entities
            GROUP BY COALESCE(entity_type, 'other')
            ORDER BY count DESC, entity_type
            """
        )
        types = [
            {"entity_type": row["entity_type"], "count": int(row["count"])}
            for row in type_rows
        ]
    entities = [entity_index_card(row) for row in rows]
    if q:
        entities = [
            entity
            for entity in entities
            if q in " ".join(
                [
                    str(entity.get("name") or ""),
                    str(entity.get("entity_type") or ""),
                    " ".join(entity.get("aliases") or []),
                ]
            ).casefold()
        ]
    return {"entities": entities, "count": len(entities), "types": types}


def entity_index_card(row: dict[str, Any]) -> dict[str, Any]:
    aliases = parse_stored_list(row.get("aliases"))
    return {
        "id": row["id"],
        "name": row["name"],
        "entity_type": row.get("entity_type") or "other",
        "aliases": aliases,
        "alias_count": len(aliases),
        "status": row.get("status") or "active",
        "merged_into": row.get("merged_into"),
        "fact_count": int(row.get("fact_count") or 0),
        "last_observed_at": row.get("last_observed_at"),
        "created_at": row.get("created_at"),
    }


def ui_entity_detail(paths: BrainPaths, entity_id: str) -> dict[str, Any]:
    service(paths).init_workspace()
    with connection(paths.sqlite_path) as conn:
        row = conn.execute("SELECT * FROM entities WHERE id = ?", (entity_id,)).fetchone()
        if not row:
            raise NotFoundError(f"entity not found: {entity_id}")
        entity = entity_index_card({**row_to_plain_dict(row), "fact_count": 0})
        facts = entity_facts(conn, entity_id)
        co_mentions = entity_co_mentions(conn, entity_id)
    enriched_facts = [enrich_fact_like(paths, fact) for fact in facts]
    grouped: dict[str, list[dict[str, Any]]] = {}
    for fact in enriched_facts:
        grouped.setdefault(str(fact.get("page_hint") or "(unrouted)"), []).append(fact)
    merge_candidates = [
        candidate
        for candidate in deterministic_entity_candidates(paths)
        if entity_id in {str(item) for item in candidate.get("entity_ids") or []}
    ][:20]
    entity["fact_count"] = len(enriched_facts)
    return {
        "entity": entity,
        "facts_by_page": [
            {"page_hint": page_hint, "facts": page_facts}
            for page_hint, page_facts in sorted(grouped.items())
        ],
        "co_mentions": co_mentions,
        "merge_candidates": merge_candidates,
    }


def entity_facts(conn: Any, entity_id: str) -> list[dict[str, Any]]:
    if not ui_table_exists(conn, "fact_entities"):
        return []
    rows = conn.execute(
        """
        SELECT DISTINCT f.*
        FROM facts f
        JOIN fact_entities fe ON fe.fact_id = f.id
        WHERE fe.entity_id = ?
          AND f.status IN ('active', 'conflicted')
        ORDER BY COALESCE(f.observed_at, f.created_at) DESC
        LIMIT 500
        """,
        (entity_id,),
    )
    return [row_to_fact(row) for row in rows]


def entity_co_mentions(conn: Any, entity_id: str) -> list[dict[str, Any]]:
    if not ui_table_exists(conn, "fact_entities"):
        return []
    rows = conn.execute(
        """
        SELECT e.id, e.name, e.entity_type, COUNT(DISTINCT fe2.fact_id) AS count
        FROM fact_entities fe1
        JOIN fact_entities fe2 ON fe2.fact_id = fe1.fact_id
        JOIN entities e ON e.id = fe2.entity_id
        WHERE fe1.entity_id = ?
          AND fe2.entity_id != ?
          AND COALESCE(e.status, 'active') = 'active'
        GROUP BY e.id
        ORDER BY count DESC, e.name
        LIMIT 50
        """,
        (entity_id, entity_id),
    )
    return [
        {
            "id": row["id"],
            "name": row["name"],
            "entity_type": row["entity_type"],
            "count": int(row["count"]),
        }
        for row in rows
    ]


def ui_entities_merge(paths: BrainPaths, payload: dict[str, Any]) -> dict[str, Any]:
    service(paths).init_workspace()
    candidate = payload.get("candidate")
    if isinstance(candidate, dict) and candidate.get("action_type") == "entity_merge":
        proposed = propose_gardener_action(paths, candidate)
        action = decide_action(paths, proposed["id"])
        return {"action": action}
    canonical_id = str(payload.get("canonical_entity_id") or "").strip()
    merged_ids = [
        str(entity_id).strip()
        for entity_id in payload.get("merged_entity_ids") or []
        if str(entity_id).strip()
    ]
    if not canonical_id or not merged_ids:
        raise BadRequestError("canonical_entity_id and merged_entity_ids are required")
    entity_ids = [canonical_id, *merged_ids]
    features = entity_merge_features(paths, entity_ids)
    action = decide_action(
        paths,
        propose_action(
            paths,
            "entity_merge",
            action_payload={
                "canonical_entity_id": canonical_id,
                "merged_entity_ids": merged_ids,
                "reason": payload.get("reason") or "manual UI merge proposal",
            },
            action_features=features,
            target_fact_ids=features["target_fact_ids"],
            proposed_by="ui_entities",
            confidence=float(payload.get("confidence") or 1.0),
            risk_tier=str(payload.get("risk_tier") or features.get("risk_tier") or "medium"),
        )["id"],
    )
    return {"action": action}


def entity_merge_features(paths: BrainPaths, entity_ids: list[str]) -> dict[str, Any]:
    with connection(paths.sqlite_path) as conn:
        placeholders = ",".join("?" for _ in entity_ids)
        rows = list(
            conn.execute(
                f"SELECT id, entity_type FROM entities WHERE id IN ({placeholders})",
                entity_ids,
            )
        )
        fact_ids = [
            str(row["fact_id"])
            for row in conn.execute(
                f"""
                SELECT DISTINCT fact_id
                FROM fact_entities
                WHERE entity_id IN ({placeholders})
                """,
                entity_ids,
            )
        ]
    types = {str(row["entity_type"] or "") for row in rows if row["entity_type"]}
    return {
        "target_fact_ids": fact_ids,
        "affected_fact_count": len(fact_ids),
        "merged_entity_count": len(entity_ids),
        "cross_type_merge": len(types) > 1,
        "cross_entity_merge": False,
        "reversible": True,
        "risk_tier": "high" if len(types) > 1 else "medium",
    }


def ui_revert_action(paths: BrainPaths, action_id: str) -> dict[str, Any]:
    service(paths).init_workspace()
    return {"action": revert_action(paths, action_id)}


def ui_ops_runs(paths: BrainPaths) -> dict[str, Any]:
    service(paths).init_workspace()
    with connection(paths.sqlite_path) as conn:
        automation = (
            [
                row_to_plain_dict(row)
                for row in conn.execute(
                    """
                    SELECT *
                    FROM automation_runs
                    ORDER BY started_at DESC
                    LIMIT 100
                    """
                )
            ]
            if ui_table_exists(conn, "automation_runs")
            else []
        )
        ingestion = (
            [
                row_to_plain_dict(row)
                for row in conn.execute(
                    """
                    SELECT *
                    FROM ingestion_runs
                    ORDER BY started_at DESC
                    LIMIT 100
                    """
                )
            ]
            if ui_table_exists(conn, "ingestion_runs")
            else []
        )
    return {"automation_runs": automation, "ingestion_runs": ingestion}


def ui_wiki_fact_dashboard(paths: BrainPaths) -> dict[str, Any]:
    service(paths).init_workspace()
    return wiki_fact_dashboard(paths)


def ui_cos_policy(paths: BrainPaths) -> dict[str, Any]:
    service(paths).init_workspace()
    with connection(paths.sqlite_path) as conn:
        rules = [dict(row) for row in active_policy_rules(conn)]
        version = active_policy_version(conn)
    for rule in rules:
        rule["match_action_types"] = json_loads(rule.get("match_action_types"), [])
        rule["match_predicate"] = json_loads(rule.get("match_predicate"), {})
        rule["auto_revert_signals"] = json_loads(rule.get("auto_revert_signals"), [])
    return {"version": version, "rules": rules}


def ui_cos_actions(paths: BrainPaths, query: dict[str, list[str]]) -> dict[str, Any]:
    service(paths).init_workspace()
    limit = int(first(query, "limit") or 50)
    return {"actions": recent_actions(paths, limit=max(1, min(limit, 200)))}


def ui_cos_review(
    paths: BrainPaths, query: dict[str, list[str]] | None = None
) -> dict[str, Any]:
    service(paths).init_workspace()
    kind_filter = first(query or {}, "kind")
    residue_where = "status IN ('open', 'needs_human')"
    residue_params: list[Any] = []
    if kind_filter:
        residue_where = f"{residue_where} AND kind = ?"
        residue_params.append(kind_filter)
    with connection(paths.sqlite_path) as conn:
        policy_version = active_policy_version(conn)
        residue = [
            row_to_question(row)
            for row in conn.execute(
                f"""
                SELECT *
                FROM open_questions
                WHERE {residue_where}
                ORDER BY
                  CASE kind
                    WHEN 'fact_conflict_review' THEN 0
                    WHEN 'conflict' THEN 1
                    WHEN 'unrouted_fact' THEN 2
                    WHEN 'document_extraction_anomaly' THEN 3
                    WHEN 'policy_escalation' THEN 4
                    ELSE 5
                  END,
                  CASE risk_tier WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END,
                  created_at DESC
                LIMIT 200
                """,
                residue_params,
            )
        ]
        recent_auto_applied = [
            row_to_action(row)
            for row in conn.execute(
                """
                SELECT *
                FROM cos_actions
                WHERE status IN ('auto_applied', 'applied')
                ORDER BY COALESCE(applied_at, created_at) DESC
                LIMIT 25
                """
            )
        ]
        audit_failures = [
            row_to_action(row)
            for row in conn.execute(
                """
                SELECT *
                FROM cos_actions
                WHERE audit_status = 'sampled_bad' OR status = 'failed'
                ORDER BY COALESCE(applied_at, created_at) DESC
                LIMIT 25
                """
            )
        ]
        residue_by_kind = {
            str(row["kind"]): int(row["count"])
            for row in conn.execute(
                """
                SELECT kind, COUNT(*) AS count
                FROM open_questions
                WHERE status IN ('open', 'needs_human')
                GROUP BY kind
                """
            )
        }
        residue_by_status = {
            str(row["status"]): int(row["count"])
            for row in conn.execute(
                """
                SELECT status, COUNT(*) AS count
                FROM open_questions
                WHERE status IN ('open', 'needs_human')
                GROUP BY status
                """
            )
        }
    return {
        "policy_version": policy_version,
        "selected_kind": kind_filter,
        "counts": {
            "residue": sum(residue_by_kind.values()),
            "recent_auto_applied": len(recent_auto_applied),
            "audit_failures": len(audit_failures),
            "residue_by_kind": dict(sorted(residue_by_kind.items())),
            "residue_by_status": dict(sorted(residue_by_status.items())),
        },
        "residue": residue,
        "recent_auto_applied": recent_auto_applied,
        "audit_failures": audit_failures,
    }


def ui_cos_contracts(paths: BrainPaths) -> dict[str, Any]:
    service(paths).init_workspace()
    return {"contracts": active_page_contracts(paths)}


def ui_cos_audit_status(paths: BrainPaths) -> dict[str, Any]:
    service(paths).init_workspace()
    with connection(paths.sqlite_path) as conn:
        counts = {
            str(row["audit_status"]): int(row["count"])
            for row in conn.execute(
                """
                SELECT audit_status, COUNT(*) AS count
                FROM cos_actions
                GROUP BY audit_status
                """
            )
        }
        failures = [
            dict(row)
            for row in conn.execute(
                """
                SELECT *
                FROM cos_actions
                WHERE audit_status = 'sampled_bad'
                ORDER BY applied_at DESC
                LIMIT 50
                """
            )
        ]
    has_configured_audits = bool(counts.get("sampled_ok") or counts.get("sampled_bad"))
    return {
        "status": "ok" if has_configured_audits else "stub",
        "mode": "configured" if has_configured_audits else "stub",
        "note": COS_AUDIT_CONFIGURED_NOTE if has_configured_audits else COS_AUDIT_STUB_NOTE,
        "counts": counts,
        "failures": failures,
    }


def ui_generate_cos_contracts(paths: BrainPaths, payload: dict[str, Any]) -> dict[str, Any]:
    service(paths).init_workspace()
    return generate_initial_contracts(
        paths,
        limit=int(payload["limit"]) if payload.get("limit") is not None else None,
        apply=bool(payload.get("apply", False)),
    )


def ui_run_cos_audit(paths: BrainPaths, payload: dict[str, Any]) -> dict[str, Any]:
    service(paths).init_workspace()
    return run_sampled_audit(
        paths,
        limit=int(payload.get("limit") or 25),
        auto_revert_bad=bool(payload.get("auto_revert_bad", False)),
        provider=str(payload["provider"]) if payload.get("provider") else None,
    )


def ui_wiki_fact_page_review(
    paths: BrainPaths, query: dict[str, list[str]]
) -> dict[str, Any]:
    service(paths).init_workspace()
    page_hint = first(query, "path")
    if not page_hint:
        raise BadRequestError("path is required")
    try:
        return managed_fact_page_review(paths, page_hint)
    except ValueError as exc:
        raise BadRequestError(str(exc)) from exc


def ui_answer_wiki_question(
    paths: BrainPaths, question_id: str, payload: dict[str, Any]
) -> dict[str, Any]:
    service(paths).init_workspace()
    try:
        return answer_open_question(
            paths,
            question_id,
            selected_fact_id=optional_str(payload.get("selected_fact_id")),
            answer=optional_str(payload.get("answer")),
            overwrite_existing=bool(payload.get("overwrite_existing", False)),
        )
    except ValueError as exc:
        raise BadRequestError(str(exc)) from exc


def ui_apply_cos_question_action(
    paths: BrainPaths, question_id: str, payload: dict[str, Any]
) -> dict[str, Any]:
    service(paths).init_workspace()
    question = review_question_for_decision(paths, question_id)
    action_id = str(question.get("action_id") or "").strip()
    if not action_id:
        raise BadRequestError(f"review question has no linked action: {question_id}")
    action = apply_action(paths, action_id)
    answer_payload = {
        "decision": "apply_action",
        "action_id": action_id,
        "note": optional_str(payload.get("note")) or "",
    }
    mark_review_question_decided(
        paths,
        question_id,
        status="answered",
        answer=answer_payload,
        action_id=action_id,
    )
    return {
        "question": get_review_question(paths, question_id),
        "action": action,
        "review": ui_cos_review(paths, review_query_for_question(question)),
        "dashboard": wiki_fact_dashboard(paths),
    }


def ui_dismiss_cos_question(
    paths: BrainPaths, question_id: str, payload: dict[str, Any]
) -> dict[str, Any]:
    service(paths).init_workspace()
    question = review_question_for_decision(paths, question_id)
    action_id = str(question.get("action_id") or "").strip()
    reason = optional_str(payload.get("reason")) or "human rejected review item"
    action: dict[str, Any] | None = None
    if action_id:
        action = reject_linked_review_action(paths, action_id, reason)
    answer_payload = {
        "decision": "dismiss",
        "action_id": action_id,
        "reason": reason,
    }
    mark_review_question_decided(
        paths,
        question_id,
        status="dismissed",
        answer=answer_payload,
        action_id=action_id or None,
    )
    return {
        "question": get_review_question(paths, question_id),
        "action": action,
        "review": ui_cos_review(paths, review_query_for_question(question)),
        "dashboard": wiki_fact_dashboard(paths),
    }


def review_question_for_decision(paths: BrainPaths, question_id: str) -> dict[str, Any]:
    question = get_review_question(paths, question_id)
    if question["status"] not in {"open", "needs_human"}:
        raise BadRequestError(f"review question is already closed: {question_id}")
    return question


def review_query_for_question(question: dict[str, Any]) -> dict[str, list[str]]:
    kind = str(question.get("kind") or "").strip()
    return {"kind": [kind]} if kind else {}


def get_review_question(paths: BrainPaths, question_id: str) -> dict[str, Any]:
    with connection(paths.sqlite_path) as conn:
        row = conn.execute(
            "SELECT * FROM open_questions WHERE id = ?", (question_id,)
        ).fetchone()
    if not row:
        raise NotFoundError(f"review question not found: {question_id}")
    return row_to_question(row)


def mark_review_question_decided(
    paths: BrainPaths,
    question_id: str,
    *,
    status: str,
    answer: dict[str, Any],
    action_id: str | None,
) -> None:
    with connection(paths.sqlite_path) as conn:
        conn.execute(
            """
            UPDATE open_questions
            SET status = ?, answer = ?, answered_at = ?, action_id = COALESCE(?, action_id),
                decided_by = 'human'
            WHERE id = ?
            """,
            (status, dumps(answer), now_iso(), action_id, question_id),
        )


def reject_linked_review_action(
    paths: BrainPaths, action_id: str, reason: str
) -> dict[str, Any]:
    timestamp = now_iso()
    with connection(paths.sqlite_path) as conn:
        action = load_action(conn, action_id)
        if action["status"] in {"applied", "auto_applied", "reverted"}:
            raise BadRequestError(
                f"linked action is already {action['status']}: {action_id}"
            )
        evidence = dict(action.get("evidence_json") or {})
        evidence["human_review"] = {
            "decision": "reject",
            "reason": reason,
            "decided_at": timestamp,
        }
        conn.execute(
            """
            UPDATE cos_actions
            SET status = 'rejected', evidence_json = ?
            WHERE id = ?
            """,
            (dumps(evidence), action_id),
        )
    with connection(paths.sqlite_path) as conn:
        return load_action(conn, action_id)


def ui_reconcile_wiki_facts(
    paths: BrainPaths, payload: dict[str, Any]
) -> dict[str, Any]:
    service(paths).init_workspace()
    try:
        return reconcile_open_fact_questions(
            paths,
            overwrite_existing=bool(payload.get("overwrite_existing", False)),
        )
    except ValueError as exc:
        raise BadRequestError(str(exc)) from exc


def ui_regenerate_wiki_fact_page(
    paths: BrainPaths, payload: dict[str, Any]
) -> dict[str, Any]:
    service(paths).init_workspace()
    page_hint = str(payload.get("page_hint") or payload.get("path") or "").strip()
    if not page_hint:
        raise BadRequestError("page_hint is required")
    try:
        return regenerate_managed_fact_page(
            paths,
            page_hint,
            dry_run=bool(payload.get("dry_run", True)),
            overwrite_existing=bool(payload.get("overwrite_existing", False)),
        )
    except ValueError as exc:
        raise BadRequestError(str(exc)) from exc


def ui_revert_wiki_fact_page(
    paths: BrainPaths, payload: dict[str, Any]
) -> dict[str, Any]:
    service(paths).init_workspace()
    snapshot_id = str(payload.get("snapshot_id") or "").strip()
    if not snapshot_id:
        raise BadRequestError("snapshot_id is required")
    try:
        return revert_wiki_page_snapshot(paths, snapshot_id)
    except ValueError as exc:
        raise BadRequestError(str(exc)) from exc


def ui_create_wiki_fact_correction(
    paths: BrainPaths, payload: dict[str, Any]
) -> dict[str, Any]:
    service(paths).init_workspace()
    page_hint = str(payload.get("page_hint") or payload.get("path") or "").strip()
    if not page_hint:
        raise BadRequestError("page_hint is required")
    try:
        return create_confirmed_page_fact(
            paths,
            page_hint,
            str(payload.get("statement") or ""),
            section_hint=str(payload.get("section_hint") or "Summary"),
            supersede_fact_ids=string_list(payload.get("supersede_fact_ids")),
            source_ids=string_list(payload.get("source_ids")),
            overwrite_existing=bool(payload.get("overwrite_existing", False)),
        )
    except ValueError as exc:
        raise BadRequestError(str(exc)) from exc


def source_document_summaries(
    paths: BrainPaths, source_ids: list[str]
) -> list[dict[str, Any]]:
    document_ids = []
    for source_id in source_ids:
        value = str(source_id)
        if value.startswith("document:"):
            document_id = value.removeprefix("document:")
            if document_id:
                document_ids.append(document_id)
    if not document_ids:
        return []
    placeholders = ",".join("?" for _ in document_ids)
    with connection(paths.sqlite_path) as conn:
        found = conn.execute(
            f"""
            SELECT id, title, source_type, source_path, raw_path, ingested_at
            FROM documents
            WHERE id IN ({placeholders})
            """,
            document_ids,
        )
        rows_by_id = {row["id"]: dict(row) for row in found}
    documents = []
    for document_id in document_ids:
        row = rows_by_id.get(document_id)
        if not row:
            continue
        documents.append(
            {
                "id": row["id"],
                "source_id": f"document:{row['id']}",
                "title": row["title"],
                "source_type": row["source_type"],
                "source_path": row["source_path"],
                "raw_path": row["raw_path"],
                "ingested_at": row["ingested_at"],
            }
        )
    return documents


def safe_wiki_path(
    paths: BrainPaths, relative_path: str, *, must_exist: bool = True
) -> Path:
    raw = str(relative_path or "").strip()
    if not raw:
        raise BadRequestError("wiki path is required")
    path = Path(raw)
    if path.is_absolute() or ".." in path.parts:
        raise BadRequestError(f"wiki path must be relative to wiki root: {raw}")
    if path.suffix != ".md":
        raise BadRequestError(f"wiki path must point to a Markdown file: {raw}")
    root = paths.wiki.resolve()
    target = (paths.wiki / path).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise BadRequestError(f"wiki path is outside wiki root: {raw}") from exc
    if must_exist and not target.exists():
        raise NotFoundError(f"wiki page not found: {raw}")
    return target


def ui_jobs_status() -> dict[str, Any]:
    try:
        return {"jobs": [status.as_dict() for status in LaunchdScheduler().status()]}
    except Exception as exc:
        return {"jobs": [], "error": str(exc)}


def ui_logs(paths: BrainPaths) -> dict[str, Any]:
    logs: list[dict[str, Any]] = []
    if paths.logs.exists():
        for path in sorted(paths.logs.glob("*.log")):
            logs.append(
                {"name": path.name, "path": str(path), "bytes": path.stat().st_size}
            )
    return {"logs": logs}


def service(paths: BrainPaths) -> BrainService:
    return BrainService(paths)


def safe_call(fn: Any) -> dict[str, Any]:
    try:
        return fn()
    except Exception as exc:
        return {"error": str(exc)}


def first(query: dict[str, list[str]], key: str) -> str | None:
    values = query.get(key) or []
    value = values[0].strip() if values else ""
    return value or None


def optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value).strip()
    return [text] if text else []


def json_loads(value: Any, default: Any) -> Any:
    if value in (None, ""):
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value))
    except json.JSONDecodeError:
        return default


def parse_stored_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value]
    try:
        parsed = yaml.safe_load(str(value)) or []
    except Exception:
        return []
    if not isinstance(parsed, list):
        return []
    return [str(item) for item in parsed]
