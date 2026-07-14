from __future__ import annotations

import json
import mimetypes
import os
import re
import secrets
import threading
from datetime import datetime, timezone
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
from .connector_api import dispatch_connector_post, dispatch_connector_put
from .connectors import get_connector, list_connectors
from .cos_actions import (
    action_candidate_key,
    audit_action_reviewability,
    action_payload,
    apply_action,
    decide_action,
    load_action,
    propose_action,
    recent_actions,
    record_action_audit,
    reviewable_bad_audit_actions,
    retire_open_candidate_siblings,
    revert_action,
    row_to_action,
    split_destination_page_hint,
)
from .cos_audit import COS_AUDIT_CONFIGURED_NOTE, COS_AUDIT_STUB_NOTE, run_sampled_audit
from .cos_policy import active_policy_rules, active_policy_version, human_policy_reason
from .curation_settings import load_curation_settings, update_curation_settings
from .db import connection, dumps
from .gardener import deterministic_entity_candidates, propose_gardener_action
from .app_migration import (
    build_migration_plan,
    create_runtime_backup,
    install_runtime_shims,
    retire_launch_agents,
)
from .mcp_tools import MCP_TOOL_NAMES, call_mcp_tool
from .operations_http import (
    OperationsHTTPBadRequest,
    OperationsHTTPNotFound,
    operations_evidence_payload,
    operations_meeting_packet_payload,
    operations_runs_payload,
    operations_storage_payload,
    shadow_setup_payload,
)
from .paths import BrainPaths
from .routing_coherence import (
    coherence_bonus,
    fact_document_id,
    load_document_route_priors,
)
from .review_admission import (
    load_review_admission_states,
    reconcile_review_admissions,
)
from .scheduler.launchd import LaunchdScheduler
from .service import BrainService, row_to_memory
from .setup_wizard import build_setup_plan
from .source_dates import derive_fact_source_date, document_source_date_metadata
from . import today_presentation as today_api
from .shadow_controller import ShadowTrialController, shadow_run_start_payload
from .util import new_id, now_iso
from .wiki import (
    ALLOWED_PAGE_TYPES,
    ALLOWED_STATUSES,
    GENERATED_MARKER,
    NON_ROUTABLE_PAGE_TYPES,
    is_routable_wiki_page,
    lint_wiki,
    parse_frontmatter,
)
from .wiki_facts import (
    answer_open_question,
    apply_fact_status_action,
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
    daemon_runtime_id: str | None
    daemon_started_at: str | None
    daemon_scheduler: Any | None
    daemon_operational_service: Any | None
    daemon_today_service: today_api.TodayPresentationService
    daemon_shadow_controller: ShadowTrialController | None
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
            elif path == "/api/v1/today":
                self.write_json(today_api.today_briefing_payload(self.server.daemon_today_service))
            elif path == "/api/v1/today/run":
                controller = self.server.daemon_shadow_controller
                if controller is None:
                    raise BadRequestError("Shadow runner is not available")
                self.write_json(controller.status())
            elif path == "/api/v1/today/setup":
                scheduler = self.server.daemon_scheduler
                self.write_json(shadow_setup_payload(self.server.paths, scheduler_state=scheduler.as_dict() if scheduler is not None else None))
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
            elif path == "/api/settings/curation":
                self.write_json(ui_curation_settings(self.server.paths))
            elif path == "/api/ops/runs":
                self.write_json(operations_runs_payload(self.server.paths))
            elif path == "/api/ops/storage":
                self.write_json(operations_storage_payload(self.server.paths))
            elif path == "/api/ops/evidence":
                self.write_json(operations_evidence_payload(self.server.paths, query))
            elif path.startswith("/api/ops/items/") and path.endswith("/meeting-packet"):
                item_id = path.removeprefix("/api/ops/items/").removesuffix(
                    "/meeting-packet"
                ).strip("/")
                self.write_json(
                    operations_meeting_packet_payload(self.server.paths, item_id)
                )
            else:
                self.write_json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)
        except BadRequestError as exc:
            self.write_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
        except OperationsHTTPBadRequest as exc:
            self.write_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
        except NotFoundError as exc:
            self.write_json({"error": str(exc)}, status=HTTPStatus.NOT_FOUND)
        except OperationsHTTPNotFound as exc:
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
            elif (
                len(parts) == 4
                and parts[:2] == ["scheduler", "jobs"]
                and parts[3] in {"enable", "disable"}
            ):
                self.write_json(
                    ui_scheduler_enable(
                        self.server,
                        parts[2],
                        enabled=parts[3] == "enable",
                    )
                )
            elif parts and parts[0] == "connectors":
                self.write_json(
                    dispatch_connector_post(
                        self.server.paths,
                        parts,
                        self.read_json_body(),
                    )
                )
            elif parts == ["retrieve"]:
                payload = self.read_json_body()
                self.write_json(ui_retrieve(self.server.paths, payload))
            elif parts == ["queue", "undo"]:
                payload = self.read_json_body()
                self.write_json(ui_queue_undo(self.server.paths, payload))
            elif (
                len(parts) == 5
                and parts[:3] == ["v1", "today", "items"]
                and parts[4] == "feedback"
            ):
                self.write_json(
                    today_api.today_feedback_payload(
                        self.server.daemon_today_service,
                        parts[3],
                        self.read_json_body(),
                    )
                )
            elif parts == ["v1", "today", "feedback", "missing"]:
                self.write_json(
                    today_api.today_missing_payload(
                        self.server.daemon_today_service, self.read_json_body()
                    )
                )
            elif len(parts) == 5 and parts[:3] == [
                "v1", "today", "calendar-series"
            ] and parts[4] == "restore":
                self.write_json(today_api.today_calendar_series_restore_payload(
                    self.server.daemon_today_service, parts[3]
                ))
            elif parts == ["v1", "today", "run"]:
                controller = self.server.daemon_shadow_controller
                if controller is None:
                    raise BadRequestError("Shadow runner is not available")
                self.write_json(
                    shadow_run_start_payload(controller, self.read_json_body())
                )
            elif len(parts) == 3 and parts[0] == "queue" and parts[2] == "decision":
                payload = self.read_json_body()
                self.write_json(ui_queue_decision(self.server.paths, parts[1], payload))
            elif parts == ["entities", "merge"]:
                payload = self.read_json_body()
                self.write_json(ui_entities_merge(self.server.paths, payload))
            elif len(parts) == 3 and parts[0] == "actions" and parts[2] == "revert":
                self.write_json(ui_revert_action(self.server.paths, parts[1]))
            elif (
                len(parts) == 4
                and parts[:2] == ["wiki", "facts"]
                and parts[3] == "confirm"
            ):
                self.write_json(ui_confirm_fact(self.server.paths, parts[2]))
            elif (
                len(parts) == 4
                and parts[:2] == ["wiki", "facts"]
                and parts[3] == "flag"
            ):
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
            if parts and parts[0] == "connectors":
                self.write_json(
                    dispatch_connector_put(
                        self.server.paths,
                        parts,
                        self.read_json_body(),
                    )
                )
                return
            if parts == ["settings", "curation"]:
                payload = self.read_json_body()
                self.write_json(ui_update_curation_settings(self.server.paths, payload))
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
            self.write_json(
                ui_dismiss_cos_question(self.server.paths, parts[2], payload)
            )
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
    server.daemon_runtime_id = None
    server.daemon_started_at = None
    server.daemon_scheduler = None
    server.daemon_operational_service = None
    server.daemon_today_service = today_api.UnavailableTodayPresentationService()
    server.daemon_shadow_controller = None
    server.daemon_shutdown_enabled = False
    return server


def ui_health(server: BrainUIServer) -> dict[str, Any]:
    host, port = server.server_address
    return {
        "ok": True,
        "version": server.daemon_version or package_version(),
        "runtime_id": server.daemon_runtime_id,
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
        "runtime_id": server.daemon_runtime_id,
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


def ui_scheduler_pause(
    server: BrainUIServer, payload: dict[str, Any]
) -> dict[str, Any]:
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


def ui_scheduler_enable(
    server: BrainUIServer, job_id: str, *, enabled: bool
) -> dict[str, Any]:
    scheduler = server.daemon_scheduler
    if scheduler is None:
        raise BadRequestError("scheduler is not available")
    return scheduler.set_enabled(job_id, enabled)


def ui_shutdown(server: BrainUIServer) -> dict[str, Any]:
    if not server.daemon_shutdown_enabled:
        raise BadRequestError("shutdown is only available for brain daemon")
    threading.Thread(
        target=server.shutdown, name="brain-daemon-shutdown", daemon=True
    ).start()
    return {"ok": True, "shutting_down": True}


def ui_mcp_tool(
    paths: BrainPaths, tool_name: str, payload: dict[str, Any]
) -> dict[str, Any] | list[Any]:
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
    return create_runtime_backup(
        paths, app_support_dir=path_from_payload(payload, "app_support_dir")
    )


def ui_migration_shims(payload: dict[str, Any]) -> dict[str, Any]:
    return install_runtime_shims(
        app_support_dir=path_from_payload(payload, "app_support_dir")
    )


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
    wiki_pages = (
        sum(1 for path in paths.wiki.rglob("*.md")) if paths.wiki.exists() else 0
    )
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
            conn,
            "SELECT COUNT(*) FROM memories WHERE status = 'active'",
            table="memories",
        )
        proposed_memories = scalar_count(
            conn,
            "SELECT COUNT(*) FROM memories WHERE status = 'proposed'",
            table="memories",
        )
        actions = scalar_count(
            conn, "SELECT COUNT(*) FROM cos_actions", table="cos_actions"
        )
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
    return column in {
        str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})")
    }


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
    request_as_of = queue_freshness_timestamp()
    svc = service(paths)
    svc.init_workspace()
    since = first(query, "since")
    index = safe_call(lambda: index_status(paths, svc))
    jobs = ui_jobs_status()
    sync = safe_call(svc.sync_status)
    audit = ui_cos_audit_status(paths)
    with connection(paths.sqlite_path) as conn:
        latest_run = compact_run_event(
            latest_row(conn, "automation_runs", "started_at")
        )
        facts_by_page = digest_facts_by_page(conn, since)
        reverts = digest_reverts(conn, since)
        demotions = digest_demotions(conn, since)
        eval_transitions = digest_eval_transitions(conn, since)
        raw_counts = queue_counts(conn)
        queue_summary = build_queue_summary(
            paths, conn, raw_counts=raw_counts, as_of=request_as_of
        )
        counts = legacy_queue_counts(queue_summary)
    pulse = [
        pulse_chip(
            "nightly",
            latest_run.get("status") if latest_run else None,
            latest_run.get("finished_at") or latest_run.get("started_at")
            if latest_run
            else None,
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
        "generated_at": queue_summary["as_of"],
        "since": since,
        "pulse": pulse,
        "latest_run": latest_run,
        "facts_by_page": facts_by_page,
        "reverts": reverts,
        "demotions": demotions,
        "eval_transitions": eval_transitions,
        "queue_summary": queue_summary,
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
            (metadata or {}).get("rationale") or evidence.get("reason")
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
    topology_identities: set[str] = set()
    question_action_ids: set[str] = set()
    if ui_table_exists(conn, "open_questions"):
        question_rows = list(
            conn.execute(
                """
                SELECT kind, action_id
                FROM open_questions
                WHERE status IN ('open', 'needs_human')
                """
            )
        )
        question_action_ids = {
            str(row["action_id"])
            for row in question_rows
            if str(row["action_id"] or "").strip()
        }
        actions = actions_for_ids(conn, question_action_ids)
        for row in question_rows:
            kind = str(row["kind"])
            raw[kind] = raw.get(kind, 0) + 1
            action_id = str(row["action_id"] or "").strip()
            action = actions.get(action_id)
            action_type = str((action or {}).get("action_type") or "")
            group = queue_group_for_kind(kind, action_type)
            if group == "topology" and action is not None:
                if entity_merge_action_is_queue_relevant(conn, action):
                    topology_identities.add(topology_action_identity(action))
                continue
            counts[group] = counts.get(group, 0) + 1
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
        direct_actions = queue_open_topology_actions(
            conn, exclude_action_ids=question_action_ids
        )
        for action in direct_actions:
            topology_identities.add(topology_action_identity(action))
        if topology_identities:
            counts["topology"] = len(topology_identities)
            raw["proposed_action"] = len(direct_actions)
        audit = queue_audit_count(conn)
        if audit:
            counts["audit"] = counts.get("audit", 0) + audit
            raw["audit_flagged"] = audit
    total = sum(counts.values())
    return {
        "total": total,
        "by_kind": dict(sorted(counts.items())),
        "raw": dict(sorted(raw.items())),
    }


def build_queue_summary(
    paths: BrainPaths,
    conn: Any,
    *,
    raw_counts: dict[str, Any] | None = None,
    as_of: str | None = None,
) -> dict[str, Any]:
    raw_counts = raw_counts or queue_counts(conn)
    candidate_total = max(
        int(raw_counts.get("total") or 0),
        sum(int(value) for value in (raw_counts.get("raw") or {}).values()),
    )
    items = queue_items(
        paths,
        conn,
        kind_filter="all",
        sort_mode="priority",
        limit=max(candidate_total, 1),
        cursor=0,
        candidate_total=candidate_total,
        include_popularity=False,
        include_route_candidates=False,
        apply_admission=False,
    )
    admission = reconcile_review_admissions(conn, items)
    admission_states = dict(admission["states"])
    by_kind: dict[str, int] = {}
    blocked_by_kind: dict[str, int] = {}
    deferred_by_kind: dict[str, int] = {}
    active_by_raw_kind: dict[str, int] = {}
    blocked_by_raw_kind: dict[str, int] = {}
    deferred_by_raw_kind: dict[str, int] = {}
    for item in items:
        group = str(item.get("group") or "other")
        raw_kind = str(item.get("kind") or "other")
        if admission_states.get(str(item.get("id") or "")) == "deferred":
            deferred_by_kind[group] = deferred_by_kind.get(group, 0) + 1
            deferred_by_raw_kind[raw_kind] = deferred_by_raw_kind.get(raw_kind, 0) + 1
            continue
        by_kind[group] = by_kind.get(group, 0) + 1
        active_by_raw_kind[raw_kind] = active_by_raw_kind.get(raw_kind, 0) + 1
        if item.get("approvable") is not True:
            blocked_by_kind[group] = blocked_by_kind.get(group, 0) + 1
            blocked_by_raw_kind[raw_kind] = blocked_by_raw_kind.get(raw_kind, 0) + 1
    blocked_total = sum(blocked_by_kind.values())
    deferred_total = sum(deferred_by_kind.values())
    active_total = len(items) - deferred_total
    return {
        "as_of": as_of or queue_freshness_timestamp(),
        "server_pid": os.getpid(),
        "home": str(paths.home),
        "active_total": active_total,
        "actionable_total": max(0, active_total - blocked_total),
        "blocked_total": blocked_total,
        "deferred_total": deferred_total,
        "active_limit": int(admission["active_limit"]),
        "daily_admission_limit": int(admission["daily_limit"]),
        "admitted_today": int(admission["admitted_today"]),
        "by_kind": dict(sorted(by_kind.items())),
        "blocked_by_kind": dict(sorted(blocked_by_kind.items())),
        "deferred_by_kind": dict(sorted(deferred_by_kind.items())),
        "active_by_raw_kind": dict(sorted(active_by_raw_kind.items())),
        "blocked_by_raw_kind": dict(sorted(blocked_by_raw_kind.items())),
        "deferred_by_raw_kind": dict(sorted(deferred_by_raw_kind.items())),
        "raw": dict(sorted((raw_counts.get("raw") or {}).items())),
    }


def legacy_queue_counts(summary: dict[str, Any]) -> dict[str, Any]:
    return {
        "total": int(summary.get("active_total") or 0),
        "by_kind": dict(summary.get("by_kind") or {}),
        "raw": dict(summary.get("raw") or {}),
    }


def queue_counts_for_state(
    summary: dict[str, Any], review_state: str
) -> dict[str, Any]:
    normalized = normalize_queue_state(review_state)
    if normalized == "all":
        return legacy_queue_counts(summary)
    active_by_kind = dict(summary.get("by_kind") or {})
    blocked_by_kind = dict(summary.get("blocked_by_kind") or {})
    deferred_by_kind = dict(summary.get("deferred_by_kind") or {})
    active_by_raw = dict(summary.get("active_by_raw_kind") or {})
    blocked_by_raw = dict(summary.get("blocked_by_raw_kind") or {})
    deferred_by_raw = dict(summary.get("deferred_by_raw_kind") or {})
    if normalized == "blocked":
        by_kind = blocked_by_kind
        raw = blocked_by_raw
    elif normalized == "deferred":
        by_kind = deferred_by_kind
        raw = deferred_by_raw
    else:
        by_kind = {
            kind: max(0, int(count) - int(blocked_by_kind.get(kind) or 0))
            for kind, count in active_by_kind.items()
        }
        raw = {
            kind: max(0, int(count) - int(blocked_by_raw.get(kind) or 0))
            for kind, count in active_by_raw.items()
        }
    by_kind = {kind: count for kind, count in by_kind.items() if count}
    raw = {kind: count for kind, count in raw.items() if count}
    return {
        "total": sum(by_kind.values()),
        "by_kind": dict(sorted(by_kind.items())),
        "raw": dict(sorted(raw.items())),
    }


def current_queue_summary(paths: BrainPaths) -> dict[str, Any]:
    with connection(paths.sqlite_path) as conn:
        return build_queue_summary(paths, conn)


def queue_freshness_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def queue_group_for_kind(kind: str, action_type: str | None = None) -> str:
    normalized = str(kind or "").strip()
    if normalized in {"fact_conflict_review", "conflict"}:
        return "conflicts"
    if normalized in {"unrouted_fact", "unrouted_inbox_batch"}:
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
    if normalized == "policy_escalation":
        return "policy"
    return normalized or "other"


def actions_for_ids(conn: Any, action_ids: set[str]) -> dict[str, dict[str, Any]]:
    if not action_ids or not ui_table_exists(conn, "cos_actions"):
        return {}
    placeholders = ",".join("?" for _ in action_ids)
    return {
        str(row["id"]): row_to_action(row)
        for row in conn.execute(
            f"SELECT * FROM cos_actions WHERE id IN ({placeholders})",
            sorted(action_ids),
        )
    }


def topology_action_identity(action: dict[str, Any]) -> str:
    return action_candidate_key(action) or f"action:{action.get('id')}"


TOPOLOGY_ACTION_TYPES = set(
    "page_merge page_split rename_page archive_page rehome_fact edit_contract "
    "entity_merge entity_split synthesize_page".split()
)


class QueueBuildContext:
    def __init__(
        self, paths: BrainPaths, conn: Any, *, include_popularity: bool = True
    ):
        self.paths = paths
        self.conn = conn
        self.include_popularity = include_popularity
        self._active_pages: list[dict[str, Any]] | None = None
        self._action_cache: dict[str, dict[str, Any]] = {}
        self._fact_cache: dict[str, dict[str, Any]] = {}
        self._entity_cache: dict[str, dict[str, Any] | None] = {}
        self._source_document_cache: dict[str, dict[str, Any] | None] = {}
        self._chunk_document_cache: dict[str, str | None] = {}
        self._document_route_prior_cache: dict[str, list[dict[str, Any]]] = {}
        self._fact_retrieval_events: dict[str, dict[str, str]] | None = None
        self._entity_retrieval_events: dict[str, dict[str, str]] | None = None

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

    def entities(self, entity_ids: list[str]) -> dict[str, dict[str, Any]]:
        requested = [entity_id for entity_id in entity_ids if entity_id]
        missing = [
            entity_id for entity_id in requested if entity_id not in self._entity_cache
        ]
        if missing and ui_table_exists(self.conn, "entities"):
            placeholders = ",".join("?" for _ in missing)
            rows = self.conn.execute(
                f"""
                SELECT id, name, entity_type, status
                FROM entities
                WHERE id IN ({placeholders})
                """,
                missing,
            )
            found = {str(row["id"]): dict(row) for row in rows}
            for entity_id in missing:
                self._entity_cache[entity_id] = found.get(entity_id)
        return {
            entity_id: row
            for entity_id, row in self._entity_cache.items()
            if entity_id in requested and row
        }

    def source_documents(self, source_ids: list[str]) -> list[dict[str, Any]]:
        normalized = [str(source_id) for source_id in source_ids if source_id]
        chunk_ids = [
            source_id.removeprefix("chunk:")
            for source_id in normalized
            if source_id.startswith("chunk:") and source_id.removeprefix("chunk:")
        ]
        missing_chunks = [
            chunk_id
            for chunk_id in chunk_ids
            if chunk_id not in self._chunk_document_cache
        ]
        if missing_chunks and ui_table_exists(self.conn, "chunks"):
            placeholders = ",".join("?" for _ in missing_chunks)
            rows = self.conn.execute(
                f"SELECT id, document_id FROM chunks WHERE id IN ({placeholders})",
                missing_chunks,
            )
            found = {str(row["id"]): str(row["document_id"]) for row in rows}
            for chunk_id in missing_chunks:
                self._chunk_document_cache[chunk_id] = found.get(chunk_id)

        document_ids: list[str] = []
        references: dict[str, list[str]] = {}
        for source_id in normalized:
            document_id = ""
            if source_id.startswith("document:"):
                document_id = source_id.removeprefix("document:")
            elif source_id.startswith("chunk:"):
                document_id = str(
                    self._chunk_document_cache.get(source_id.removeprefix("chunk:"))
                    or ""
                )
            if not document_id:
                continue
            if document_id not in document_ids:
                document_ids.append(document_id)
            references.setdefault(document_id, []).append(source_id)
        missing = [
            document_id
            for document_id in document_ids
            if document_id not in self._source_document_cache
        ]
        if missing and ui_table_exists(self.conn, "documents"):
            placeholders = ",".join("?" for _ in missing)
            rows = self.conn.execute(
                f"""
                SELECT id, title, source_type, source_path, raw_path,
                       created_at, ingested_at
                FROM documents
                WHERE id IN ({placeholders})
                """,
                missing,
            )
            found = {}
            for row in rows:
                document = dict(row)
                document.update(document_source_date_metadata(document))
                found[str(row["id"])] = document
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
                    "created_at": row["created_at"],
                    "ingested_at": row["ingested_at"],
                    "source_date": row.get("source_date"),
                    "source_date_basis": row.get("source_date_basis"),
                    "source_refs": references.get(document_id, []),
                }
            )
        return documents

    def fact_popularity(self, fact_ids: list[str]) -> dict[str, Any]:
        if self._fact_retrieval_events is None:
            self._fact_retrieval_events = fact_retrieval_event_index(self.conn)
        return popularity_for_ids(self._fact_retrieval_events, fact_ids)

    def document_route_priors(self, document_id: str) -> list[dict[str, Any]]:
        normalized = str(document_id or "").strip()
        if normalized not in self._document_route_prior_cache:
            self._document_route_prior_cache[normalized] = (
                load_document_route_priors(self.conn, normalized) if normalized else []
            )
        return self._document_route_prior_cache[normalized]

    def descriptor_popularity(self, descriptor: dict[str, Any]) -> dict[str, Any]:
        if self._fact_retrieval_events is None:
            self._fact_retrieval_events = fact_retrieval_event_index(self.conn)
        if self._entity_retrieval_events is None:
            self._entity_retrieval_events = entity_retrieval_event_index(self.conn)
        fact_ids: set[str] = set()
        entity_ids: set[str] = set()
        collect_popularity_target_ids(descriptor.get("item"), fact_ids, entity_ids)
        if descriptor.get("source_type") == "question":
            action_id = str(
                (descriptor.get("item") or {}).get("action_id") or ""
            ).strip()
            if action_id:
                try:
                    collect_popularity_target_ids(
                        self.action(action_id), fact_ids, entity_ids
                    )
                except ValueError:
                    pass
        fact = popularity_for_ids(self._fact_retrieval_events, sorted(fact_ids))
        entity = popularity_for_ids(self._entity_retrieval_events, sorted(entity_ids))
        event_timestamps = {
            **popularity_events_for_ids(self._fact_retrieval_events, sorted(fact_ids)),
            **popularity_events_for_ids(
                self._entity_retrieval_events, sorted(entity_ids)
            ),
        }
        return {
            "retrieval_count": len(event_timestamps),
            "last_retrieved_at": max(event_timestamps.values())
            if event_timestamps
            else None,
            "fact_retrieval_count": fact["retrieval_count"],
            "entity_retrieval_count": entity["retrieval_count"],
        }


FACT_ID_KEYS = {"fact_id", "supersedes_id"}
FACT_IDS_KEYS = {"fact_ids", "target_fact_ids", "counterpart_fact_ids"}
ENTITY_ID_KEYS = {
    "entity_id",
    "canonical_entity_id",
    "target_entity_id",
    "destination_entity_id",
}
ENTITY_IDS_KEYS = {"entity_ids", "merged_entity_ids", "source_entity_ids"}


def fact_retrieval_event_index(conn: Any) -> dict[str, dict[str, str]]:
    if not ui_table_exists(conn, "context_lineage_events"):
        return {}
    rows = conn.execute(
        """
        SELECT target_id, retrieval_event_id, created_at
        FROM context_lineage_events
        WHERE target_type = 'fact'
          AND event_type = 'exposed'
          AND retrieval_event_id IS NOT NULL
        """
    )
    index: dict[str, dict[str, str]] = {}
    for row in rows:
        index.setdefault(str(row["target_id"]), {})[str(row["retrieval_event_id"])] = (
            str(row["created_at"] or "")
        )
    return index


def entity_retrieval_event_index(conn: Any) -> dict[str, dict[str, str]]:
    if not ui_table_exists(conn, "context_lineage_events") or not ui_table_exists(
        conn, "fact_entities"
    ):
        return {}
    rows = conn.execute(
        """
        SELECT fe.entity_id, cle.retrieval_event_id, cle.created_at
        FROM fact_entities fe
        JOIN context_lineage_events cle
          ON cle.target_type = 'fact'
         AND cle.target_id = fe.fact_id
         AND cle.event_type = 'exposed'
         AND cle.retrieval_event_id IS NOT NULL
        """
    )
    index: dict[str, dict[str, str]] = {}
    for row in rows:
        index.setdefault(str(row["entity_id"]), {})[str(row["retrieval_event_id"])] = (
            str(row["created_at"] or "")
        )
    return index


def popularity_events_for_ids(
    index: dict[str, dict[str, str]], target_ids: list[str]
) -> dict[str, str]:
    events: dict[str, str] = {}
    for target_id in target_ids:
        for event_id, created_at in index.get(str(target_id), {}).items():
            if created_at >= events.get(event_id, ""):
                events[event_id] = created_at
    return events


def popularity_for_ids(
    index: dict[str, dict[str, str]], target_ids: list[str]
) -> dict[str, Any]:
    events = popularity_events_for_ids(index, target_ids)
    return {
        "retrieval_count": len(events),
        "last_retrieved_at": max(events.values()) if events else None,
    }


def collect_popularity_target_ids(
    value: Any, fact_ids: set[str], entity_ids: set[str]
) -> None:
    if isinstance(value, list):
        for item in value:
            collect_popularity_target_ids(item, fact_ids, entity_ids)
        return
    if not isinstance(value, dict):
        return
    for key, item in value.items():
        if key in FACT_ID_KEYS:
            if str(item or "").strip():
                fact_ids.add(str(item).strip())
            continue
        if key in FACT_IDS_KEYS:
            values = item if isinstance(item, list) else [item]
            fact_ids.update(
                str(target).strip() for target in values if str(target or "").strip()
            )
            continue
        if key in ENTITY_ID_KEYS:
            if str(item or "").strip():
                entity_ids.add(str(item).strip())
            continue
        if key in ENTITY_IDS_KEYS:
            values = item if isinstance(item, list) else [item]
            entity_ids.update(
                str(target).strip() for target in values if str(target or "").strip()
            )
            continue
        collect_popularity_target_ids(item, fact_ids, entity_ids)


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
    return queue_open_topology_actions(conn, exclude_action_ids=exclude_action_ids)[
        :limit
    ]


def queue_open_topology_actions(
    conn: Any, *, exclude_action_ids: set[str]
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
          created_at DESC,
          id DESC
        """,
        [*TOPOLOGY_ACTION_TYPES, *params],
    )
    actions: list[dict[str, Any]] = []
    seen_candidate_keys: set[str] = set()
    for row in rows:
        action = row_to_action(row)
        if not entity_merge_action_is_queue_relevant(conn, action):
            continue
        candidate_key = action_candidate_key(action)
        if candidate_key and candidate_key in seen_candidate_keys:
            continue
        if candidate_key:
            seen_candidate_keys.add(candidate_key)
        actions.append(action)
    return actions


def entity_merge_action_is_queue_relevant(conn: Any, action: dict[str, Any]) -> bool:
    if action.get("action_type") != "entity_merge":
        return True
    payload = action_payload(action)
    canonical_id = str(
        payload.get("canonical_entity_id")
        or payload.get("target_entity_id")
        or payload.get("destination_entity_id")
        or ""
    ).strip()
    raw_sources = (
        payload.get("merged_entity_ids")
        or payload.get("source_entity_ids")
        or payload.get("entity_ids")
        or []
    )
    if not isinstance(raw_sources, list):
        raw_sources = [raw_sources]
    source_ids = [
        str(value).strip()
        for value in raw_sources
        if str(value or "").strip() and str(value).strip() != canonical_id
    ]
    if not canonical_id or not source_ids or not ui_table_exists(conn, "entities"):
        return True
    target_ids = [canonical_id, *source_ids]
    placeholders = ",".join("?" for _ in target_ids)
    entities = {
        str(row["id"]): row
        for row in conn.execute(
            f"SELECT id, status FROM entities WHERE id IN ({placeholders})", target_ids
        )
    }
    if len(entities) != len(target_ids):
        return True
    return all(
        str(entities[target_id]["status"] or "") == "active" for target_id in target_ids
    )


def queue_audit_rows(conn: Any, *, limit: int) -> list[dict[str, Any]]:
    if not ui_table_exists(conn, "cos_actions"):
        return []
    return reviewable_bad_audit_actions(conn)[:limit]


def queue_audit_count(conn: Any) -> int:
    if not ui_table_exists(conn, "cos_actions"):
        return 0
    return len(reviewable_bad_audit_actions(conn))


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
    request_as_of = queue_freshness_timestamp()
    kind_filter = first(query, "kind") or "all"
    review_state = normalize_queue_state(first(query, "state") or "actionable")
    sort_mode = normalize_queue_sort(first(query, "sort") or "priority")
    limit = bounded_int(first(query, "limit"), default=200, minimum=1, maximum=500)
    cursor = bounded_int(first(query, "cursor"), default=0, minimum=0, maximum=100_000)
    with connection(paths.sqlite_path) as conn:
        raw_counts = queue_counts(conn)
        queue_summary = build_queue_summary(
            paths, conn, raw_counts=raw_counts, as_of=request_as_of
        )
        counts = queue_counts_for_state(queue_summary, review_state)
        total = queue_total_from_counts(counts, kind_filter)
        items = queue_items(
            paths,
            conn,
            kind_filter=kind_filter,
            sort_mode=sort_mode,
            limit=limit,
            cursor=cursor,
            candidate_total=max(
                int(queue_summary.get("active_total") or 0)
                + int(queue_summary.get("deferred_total") or 0),
                sum(
                    int(value)
                    for value in (
                        queue_summary.get("active_by_raw_kind") or {}
                    ).values()
                ),
            ),
            review_state=review_state,
        )
    next_cursor = cursor + limit if cursor + limit < total else None
    return {
        "kind": kind_filter,
        "state": review_state,
        "sort": sort_mode,
        "queue_summary": queue_summary,
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
    sort_mode: str,
    limit: int,
    cursor: int,
    candidate_total: int,
    review_state: str = "all",
    include_popularity: bool = True,
    include_route_candidates: bool = True,
    apply_admission: bool = True,
) -> list[dict[str, Any]]:
    fetch_limit = cursor + limit
    descriptors: list[dict[str, Any]] = []
    if fetch_limit <= 0:
        return []
    normalized_state = normalize_queue_state(review_state)
    if normalized_state != "all" or sort_mode in {"retrieval", "newest"}:
        descriptor_limit = max(fetch_limit, candidate_total)
    else:
        descriptor_limit = max(fetch_limit, min(2_000, fetch_limit * 4))
    descriptors.extend(queue_question_descriptors(conn, kind_filter, descriptor_limit))
    if queue_filter_matches(kind_filter, "proposed_action", "topology"):
        question_action_ids = active_question_action_ids(conn)
        descriptors.extend(
            {
                "source_type": "action",
                "item": action,
                "sort": queue_item_sort_key(queue_action_stub(action)),
                "dedupe_key": f"topology:{topology_action_identity(action)}",
            }
            for action in queue_action_rows(
                conn, exclude_action_ids=question_action_ids, limit=descriptor_limit
            )
        )
    if queue_filter_matches(kind_filter, "audit_flagged", "audit"):
        descriptors.extend(
            {
                "source_type": "audit",
                "item": action,
                "sort": queue_item_sort_key(queue_audit_stub(action)),
            }
            for action in queue_audit_rows(conn, limit=descriptor_limit)
        )
    if queue_filter_matches(kind_filter, "proposed_memory", "memories"):
        descriptors.extend(
            {
                "source_type": "memory",
                "item": memory,
                "sort": queue_item_sort_key(queue_memory_stub(memory)),
            }
            for memory in queue_memory_rows(conn, limit=descriptor_limit)
        )
    descriptors.sort(
        key=lambda descriptor: str(descriptor["sort"][2] or ""), reverse=True
    )
    descriptors.sort(key=lambda descriptor: descriptor["sort"][:2])
    descriptors = dedupe_queue_descriptors(descriptors)
    ctx = QueueBuildContext(paths, conn, include_popularity=include_popularity)
    if include_popularity:
        for descriptor in descriptors:
            descriptor["popularity"] = ctx.descriptor_popularity(descriptor)
    if sort_mode == "retrieval":
        descriptors.sort(
            key=lambda descriptor: int(
                (descriptor.get("popularity") or {}).get("retrieval_count") or 0
            ),
            reverse=True,
        )
    elif sort_mode == "newest":
        descriptors.sort(
            key=lambda descriptor: str(descriptor["sort"][2] or ""),
            reverse=True,
        )
    if normalized_state == "all" and not apply_admission:
        return [
            queue_item_from_descriptor(
                ctx, descriptor, include_route_candidates=include_route_candidates
            )
            for descriptor in descriptors[cursor : cursor + limit]
        ]

    admission_states = (
        load_review_admission_states(
            conn,
            {
                str(descriptor["item"].get("id") or "")
                for descriptor in descriptors
            },
        )
        if apply_admission
        else {}
    )
    matching: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for descriptor in descriptors:
        card = queue_item_from_descriptor(
            ctx, descriptor, include_route_candidates=False
        )
        admission_state = admission_states.get(str(card.get("id") or ""), "admitted")
        card["admission_state"] = admission_state
        if queue_item_matches_state(card, normalized_state, admission_state):
            matching.append((descriptor, card))
    selected = matching[cursor : cursor + limit]
    if not include_route_candidates:
        return [card for _, card in selected]
    items = []
    for descriptor, card in selected:
        complete = queue_item_from_descriptor(
            ctx, descriptor, include_route_candidates=True
        )
        complete["admission_state"] = card["admission_state"]
        items.append(complete)
    return items


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
            WHEN 'unrouted_inbox_batch' THEN 3
            WHEN 'document_extraction_anomaly' THEN 4
            WHEN 'policy_escalation' THEN 5
            ELSE 6
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
            descriptor = {
                "source_type": "question",
                "item": question,
                "sort": queue_item_sort_key(stub),
            }
            if stub.get("group") == "topology" and question.get("action_id"):
                action = actions_for_ids(conn, {str(question["action_id"])}).get(
                    str(question["action_id"])
                )
                if action is None or not entity_merge_action_is_queue_relevant(
                    conn, action
                ):
                    continue
                descriptor["dedupe_key"] = (
                    f"topology:{topology_action_identity(action)}"
                )
            descriptors.append(descriptor)
    return descriptors


def dedupe_queue_descriptors(
    descriptors: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    seen: set[str] = set()
    deduped = []
    for descriptor in descriptors:
        key = str(descriptor.get("dedupe_key") or "")
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        deduped.append(descriptor)
    return deduped


def queue_item_from_descriptor(
    ctx: QueueBuildContext,
    descriptor: dict[str, Any],
    *,
    include_route_candidates: bool = True,
) -> dict[str, Any]:
    source_type = str(descriptor["source_type"])
    if source_type == "question":
        item = queue_item_for_question(
            ctx,
            descriptor["item"],
            include_route_candidates=include_route_candidates,
        )
    elif source_type == "action":
        item = queue_item_for_action(ctx, descriptor["item"])
    elif source_type == "audit":
        item = queue_item_for_audit_action(ctx, descriptor["item"])
    elif source_type == "memory":
        item = queue_item_for_memory(ctx, descriptor["item"])
    else:
        raise ValueError(f"unsupported queue source: {source_type}")
    item["popularity"] = descriptor.get("popularity") or {
        "retrieval_count": 0,
        "last_retrieved_at": None,
        "fact_retrieval_count": 0,
        "entity_retrieval_count": 0,
    }
    item.update(validate_queue_card(item))
    return item


def validate_queue_card(item: dict[str, Any]) -> dict[str, Any]:
    group = str(item.get("group") or "")
    kind = str(item.get("kind") or "")

    def blocked(code: str, reason: str) -> dict[str, Any]:
        return {
            "approvable": False,
            "blocking_code": code,
            "blocking_reason": reason,
        }

    def fact_problem(fact: Any, label: str) -> dict[str, Any] | None:
        if not isinstance(fact, dict):
            return blocked("missing_fact", f"{label} fact payload is unavailable.")
        if not str(fact.get("statement") or "").strip():
            return blocked("missing_statement", f"{label} fact has no statement.")
        evidence = str(fact.get("evidence_quote") or fact.get("quote") or "").strip()
        if (
            not evidence
            and not fact.get("source_ids")
            and not fact.get("source_documents")
        ):
            return blocked(
                "missing_evidence", f"{label} fact has no quote or source evidence."
            )
        if not str(fact.get("source_date") or "").strip():
            return blocked(
                "missing_source_date", f"{label} fact has no auditable source date."
            )
        return None

    def topology_problem() -> dict[str, Any] | None:
        action = item.get("action") if isinstance(item.get("action"), dict) else {}
        action_type = str(action.get("action_type") or "")
        topology = (
            item.get("topology") if isinstance(item.get("topology"), dict) else {}
        )
        if not action_type:
            return blocked(
                "missing_action", "The linked topology action is unavailable."
            )
        entity_ids = [value for value in topology.get("entity_ids") or [] if value]
        page_hints = [value for value in topology.get("page_hints") or [] if value]
        if not entity_ids and not page_hints:
            return blocked(
                "missing_topology_target",
                "The topology action does not identify the entity or page it changes.",
            )
        if action_type == "page_split":
            preview = topology.get("split_preview")
            if not isinstance(preview, dict) or preview.get("approvable") is not True:
                return blocked(
                    "incomplete_split_preview",
                    "The page split has no complete resulting-page preview.",
                )
        if action_type == "page_merge" and len(page_hints) < 2:
            return blocked(
                "incomplete_merge_target",
                "The page merge must show every source page and its destination.",
            )
        if action_type == "entity_merge":
            labels = [value for value in topology.get("entity_labels") or [] if value]
            statuses = topology.get("entity_statuses") or {}
            if (
                len(entity_ids) < 2
                or len(labels) < 2
                or any(
                    statuses.get(entity_id)
                    != ("merged" if group == "audit" and index else "active")
                    for index, entity_id in enumerate(entity_ids)
                )
            ):
                return blocked(
                    "incomplete_merge_target",
                    "The entity merge must show its direction and expected entity states.",
                )
        return None

    if group == "conflicts":
        if item.get("comparison_mode") == "alternatives":
            alternatives = item.get("alternatives") or []
            if len(alternatives) < 2:
                return blocked(
                    "missing_alternatives",
                    "The historical comparison has fewer than two facts.",
                )
            for index, alternative in enumerate(alternatives, start=1):
                problem = fact_problem(alternative, f"Historical fact {index}")
                if problem:
                    return problem
        else:
            problem = fact_problem(item.get("candidate"), "Candidate")
            if problem:
                return problem
            counterparts = item.get("counterparts") or []
            if not counterparts:
                return blocked(
                    "missing_counterpart",
                    "The conflict has no existing fact to compare.",
                )
            for index, counterpart in enumerate(counterparts, start=1):
                problem = fact_problem(counterpart, f"Existing {index}")
                if problem:
                    return problem
            orientation = (
                item.get("orientation")
                if isinstance(item.get("orientation"), dict)
                else {}
            )
            if not str(orientation.get("relation") or "").strip():
                return blocked(
                    "missing_relation",
                    "The candidate/existing relation is unavailable.",
                )

    elif group == "unrouted":
        if kind == "unrouted_inbox_batch":
            question = (
                item.get("question") if isinstance(item.get("question"), dict) else {}
            )
            context = (
                question.get("context")
                if isinstance(question.get("context"), dict)
                else {}
            )
            if not context.get("source_question_ids"):
                return blocked(
                    "missing_batch_members", "The inbox batch has no source questions."
                )
        else:
            problem = fact_problem(item.get("candidate"), "Unrouted")
            if problem:
                return problem

    elif kind == "policy_escalation":
        action = item.get("action")
        if not isinstance(action, dict):
            return blocked("missing_action", "The linked policy action is unavailable.")
        if not str(item.get("summary") or "").strip():
            return blocked("missing_policy_reason", "The policy reason is unavailable.")
        if str(action.get("action_type") or "") in TOPOLOGY_ACTION_TYPES:
            problem = topology_problem()
            if problem:
                return problem
        elif (
            item.get("candidate") is not None
            or str(action.get("action_type") or "") == "fact_upsert"
        ):
            problem = fact_problem(item.get("candidate"), "Candidate")
            if problem:
                return problem

    elif group == "topology":
        problem = topology_problem()
        if problem:
            return problem

    elif group == "memories":
        memory = item.get("memory") if isinstance(item.get("memory"), dict) else {}
        if not str(memory.get("content") or "").strip():
            return blocked("missing_memory", "The proposed memory has no content.")
        if not memory.get("source_ids") and not memory.get("source_documents"):
            return blocked("missing_evidence", "The proposed memory has no provenance.")

    elif group == "audit":
        action = item.get("action") if isinstance(item.get("action"), dict) else {}
        if not action:
            return blocked("missing_action", "The audited action is unavailable.")
        if not str(item.get("summary") or "").strip():
            return blocked("missing_audit_finding", "The audit finding is unavailable.")
        if str(action.get("action_type") or "") in TOPOLOGY_ACTION_TYPES:
            problem = topology_problem()
            if problem:
                return problem
        elif action.get("action_type") == "fact_upsert":
            problem = fact_problem(item.get("candidate"), "Applied")
            if problem:
                return problem

    elif group == "anomalies":
        anomaly = item.get("anomaly") if isinstance(item.get("anomaly"), dict) else {}
        if not str(anomaly.get("document_id") or "").strip():
            return blocked(
                "missing_document", "The extraction alert has no source document."
            )
        if int(anomaly.get("reviewed_count") or 0) <= 0:
            return blocked(
                "missing_anomaly_sample",
                "The extraction alert has no reviewed fact sample.",
            )

    elif not str(item.get("summary") or item.get("title") or "").strip():
        return blocked("missing_summary", "The review item has no decision context.")

    return {"approvable": True, "blocking_code": None, "blocking_reason": None}


def queue_question_filter_sql(kind_filter: str) -> tuple[str | None, list[Any]]:
    normalized = normalize_queue_filter(kind_filter)
    if normalized == "all":
        return "", []
    if normalized == "conflicts":
        return "AND kind IN ('fact_conflict_review', 'conflict')", []
    if normalized == "unrouted":
        return "AND kind IN ('unrouted_fact', 'unrouted_inbox_batch')", []
    if normalized == "anomalies":
        return "AND kind = 'document_extraction_anomaly'", []
    if normalized == "topology":
        action_types = sorted(TOPOLOGY_ACTION_TYPES)
        placeholders = ",".join("?" for _ in action_types)
        return (
            "AND kind = 'policy_escalation' "
            f"AND action_id IN (SELECT id FROM cos_actions WHERE action_type IN ({placeholders}))",
            action_types,
        )
    if normalized == "policy":
        action_types = sorted(TOPOLOGY_ACTION_TYPES)
        placeholders = ",".join("?" for _ in action_types)
        return (
            "AND kind = 'policy_escalation' "
            "AND (action_id IS NULL OR action_id NOT IN "
            f"(SELECT id FROM cos_actions WHERE action_type IN ({placeholders})))",
            action_types,
        )
    question_kinds = {
        "fact_conflict_review",
        "conflict",
        "unrouted_fact",
        "unrouted_inbox_batch",
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
        "policy_escalation": "policy",
    }
    return aliases.get(normalized, normalized)


def normalize_queue_state(value: str) -> str:
    normalized = str(value or "actionable").strip().lower().replace("-", "_")
    aliases = {
        "review": "actionable",
        "repair": "blocked",
        "needs_repair": "blocked",
    }
    normalized = aliases.get(normalized, normalized)
    if normalized not in {"actionable", "blocked", "deferred", "all"}:
        raise BadRequestError(
            "state must be one of: actionable, blocked, deferred, all"
        )
    return normalized


def queue_item_matches_state(
    item: dict[str, Any], review_state: str, admission_state: str = "admitted"
) -> bool:
    normalized = normalize_queue_state(review_state)
    blocked = item.get("approvable") is not True
    deferred = not blocked and admission_state == "deferred"
    if normalized == "blocked":
        return blocked
    if normalized == "deferred":
        return deferred
    if normalized == "all":
        return blocked or not deferred
    return not blocked and not deferred


def normalize_queue_sort(value: str) -> str:
    normalized = str(value or "priority").strip().lower()
    aliases = {
        "impact": "retrieval",
        "popular": "retrieval",
        "popularity": "retrieval",
        "recent": "newest",
    }
    normalized = aliases.get(normalized, normalized)
    if normalized not in {"priority", "retrieval", "newest"}:
        raise BadRequestError("sort must be one of: priority, retrieval, newest")
    return normalized


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
    action_type = str(
        (question.get("recommended_action") or {}).get("action_type") or ""
    )
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
    context: BrainPaths | QueueBuildContext,
    question: dict[str, Any],
    *,
    include_route_candidates: bool = True,
) -> dict[str, Any]:
    action_type = str(
        (question.get("recommended_action") or {}).get("action_type") or ""
    )
    group = queue_group_for_kind(str(question.get("kind") or ""), action_type)
    comparison_mode = (
        "alternatives" if is_alternative_fact_comparison(question) else None
    )
    alternatives = (
        question_alternative_facts(context, question)
        if comparison_mode == "alternatives"
        else []
    )
    candidate = (
        None
        if comparison_mode == "alternatives"
        else question_candidate_fact(context, question)
    )
    counterparts = (
        []
        if comparison_mode == "alternatives"
        else question_counterpart_facts(context, question)
    )
    summary = readable_question_summary(context, question)
    orientation = (
        queue_alternative_orientation(question, alternatives)
        if comparison_mode == "alternatives"
        else queue_fact_orientation(context, question, candidate, counterparts)
    )
    title = compact_text(
        orientation.get("title")
        or (candidate or {}).get("statement")
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
        "comparison_mode": comparison_mode,
        "alternatives": alternatives,
        "orientation": orientation,
        "options": question.get("options") or [],
        "raw": question,
    }
    if question.get("kind") == "document_extraction_anomaly":
        anomaly = extraction_anomaly_summary(question)
        item["anomaly"] = anomaly
        item["title"] = compact_text(
            f"Extraction quality: {anomaly.get('document_title') or 'Unknown document'}",
            120,
        )
    if group == "unrouted" and candidate and include_route_candidates:
        item["route_candidates"] = route_candidates_for_fact(context, candidate)
    if question.get("kind") == "policy_escalation" and question.get("action_id"):
        try:
            action = get_action_for_queue(context, str(question["action_id"]))
            item["action"] = action
            if str(action.get("action_type") or "") in TOPOLOGY_ACTION_TYPES:
                payload = (action.get("evidence_json") or {}).get("payload")
                item["topology"] = action_topology(context, action, payload)
                item["title"] = action_title(context, action, payload)
                item["summary"] = action_summary(action, payload)
        except ValueError:
            pass
    return item


def extraction_anomaly_summary(question: dict[str, Any]) -> dict[str, Any]:
    context = (
        question.get("context") if isinstance(question.get("context"), dict) else {}
    )
    reviewed_action_ids = [
        str(action_id)
        for action_id in context.get("reviewed_action_ids") or []
        if str(action_id or "").strip()
    ]
    blocked_action_ids = [
        str(action_id)
        for action_id in context.get("blocked_action_ids") or []
        if str(action_id or "").strip()
    ]
    reviewed_count = len(reviewed_action_ids)
    blocked_count = len(blocked_action_ids)
    block_rate = context.get("block_rate")
    if block_rate is None and reviewed_count:
        block_rate = blocked_count / reviewed_count
    return {
        "document_id": context.get("document_id"),
        "document_title": context.get("title"),
        "reviewed_count": reviewed_count,
        "blocked_count": blocked_count,
        "block_rate": block_rate,
    }


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


def queue_fact_orientation(
    context: BrainPaths | QueueBuildContext,
    question: dict[str, Any],
    candidate: dict[str, Any] | None,
    counterparts: list[dict[str, Any]],
) -> dict[str, Any]:
    primary = candidate or (counterparts[0] if counterparts else {})
    if not primary and not question.get("page_hint") and not question.get("entity_key"):
        return {}
    relation = queue_relation_context(context, question)
    page_hint = first_nonempty(
        (candidate or {}).get("page_hint"),
        question.get("page_hint"),
        *[fact.get("page_hint") for fact in counterparts],
    )
    section_hint = first_nonempty(
        (candidate or {}).get("section_hint"),
        *[fact.get("section_hint") for fact in counterparts],
        "Summary",
    )
    entity_key = first_nonempty(
        (candidate or {}).get("entity_key"),
        question.get("entity_key"),
        *[fact.get("entity_key") for fact in counterparts],
    )
    entity_label = fact_entity_label(entity_key, page_hint)
    candidate_observed_at = first_nonempty(
        (candidate or {}).get("effective_at"),
        (candidate or {}).get("observed_at"),
        (candidate or {}).get("last_seen_at"),
    )
    existing_observed_at = newest_timestamp(
        [
            first_nonempty(
                fact.get("effective_at"),
                fact.get("observed_at"),
                fact.get("last_seen_at"),
            )
            for fact in counterparts
        ]
    )
    temporal_scope = infer_fact_temporal_scope(candidate or primary)
    existing_temporal_scope = (
        infer_fact_temporal_scope(counterparts[0]) if counterparts else ""
    )
    currentness = fact_currentness_label(
        str(relation.get("relation") or ""), temporal_scope
    )
    return {
        "title": orientation_title(entity_label, section_hint),
        "entity_label": entity_label,
        "entity_key": entity_key,
        "page_hint": page_hint,
        "section_hint": section_hint,
        "candidate_observed_at": candidate_observed_at,
        "existing_observed_at": existing_observed_at,
        "temporal_scope": temporal_scope,
        "existing_temporal_scope": existing_temporal_scope,
        "currentness": currentness,
        "relation": relation.get("relation") or "",
        "relation_confidence": relation.get("confidence"),
        "relation_rationale": relation.get("rationale") or "",
    }


def queue_alternative_orientation(
    question: dict[str, Any], alternatives: list[dict[str, Any]]
) -> dict[str, Any]:
    primary = alternatives[0] if alternatives else {}
    page_hint = first_nonempty(
        question.get("page_hint"),
        *[fact.get("page_hint") for fact in alternatives],
    )
    section_hint = first_nonempty(
        *[fact.get("section_hint") for fact in alternatives], "Summary"
    )
    entity_key = first_nonempty(
        question.get("entity_key"),
        *[fact.get("entity_key") for fact in alternatives],
    )
    entity_label = fact_entity_label(entity_key, page_hint)
    return {
        "title": orientation_title(entity_label, section_hint),
        "entity_label": entity_label,
        "entity_key": entity_key,
        "page_hint": page_hint,
        "section_hint": section_hint,
        "candidate_observed_at": None,
        "existing_observed_at": None,
        "temporal_scope": infer_fact_temporal_scope(primary),
        "existing_temporal_scope": None,
        "currentness": "Keep every fact that should remain active.",
        "relation": "contested",
        "relation_confidence": None,
        "relation_rationale": str(question.get("question") or ""),
    }


def queue_relation_context(
    context: BrainPaths | QueueBuildContext, question: dict[str, Any]
) -> dict[str, Any]:
    question_context = question.get("context") or {}
    for key in ("relation", "relation_classification"):
        value = question_context.get(key)
        if isinstance(value, dict):
            return value
        if isinstance(value, str) and value:
            return {"relation": value}
    action_id = str(question.get("action_id") or "").strip()
    if not action_id:
        return {}
    try:
        action = get_action_for_queue(context, action_id)
    except ValueError:
        return {}
    features = action.get("action_features") or {}
    resolver = (action.get("evidence_json") or {}).get("resolver_precheck") or {}
    resolver_judgment = resolver.get("resolver_judgment") or {}
    if resolver_judgment.get("decision") == "no_conflict":
        return {
            "relation": "no conflict",
            "confidence": None,
            "rationale": resolver_judgment.get("rationale") or "",
        }
    if features.get("relation"):
        return {
            "relation": features.get("relation"),
            "confidence": features.get("relation_confidence"),
            "rationale": features.get("relation_rationale") or "",
        }
    classifications = resolver.get("relation_classifications") or []
    if not isinstance(classifications, list):
        return {}
    valid = [item for item in classifications if isinstance(item, dict)]
    if not valid:
        return {}
    return max(valid, key=lambda item: float(item.get("confidence") or 0.0))


def infer_fact_temporal_scope(fact: dict[str, Any]) -> str:
    statement = str(fact.get("statement") or "").casefold()
    if not statement:
        return ""
    if re.search(r"\b(from|between)\b.+\b(to|through|until)\b", statement) or re.search(
        r"\bsince\b.+\b(until|through|present|now|today)\b", statement
    ):
        return "interval_state"
    stale_markers = (
        "last week",
        "yesterday",
        "tomorrow",
        "next week",
        "this morning",
        "this afternoon",
        "waiting on",
        "scheduled",
        "upcoming",
    )
    if any(marker in statement for marker in stale_markers):
        return "stale_observation"
    current_markers = (
        "currently",
        "right now",
        "now has",
        "still",
        "is in ",
        "is working",
        "has an offer",
        "has one offer",
        "has two",
    )
    if any(marker in statement for marker in current_markers):
        return "current_state"
    event_markers = (
        " said ",
        " worked ",
        " interviewed ",
        " met ",
        " accepted ",
        " completed ",
        " decided ",
        " launched ",
        " shipped ",
    )
    if any(marker in f" {statement} " for marker in event_markers):
        return "event"
    if fact.get("effective_at") and fact.get("observed_at"):
        return "current_state"
    return "atemporal_claim"


def fact_currentness_label(relation: str, temporal_scope: str) -> str:
    if relation == "updates":
        return "candidate becomes current; existing becomes historical"
    labels = {
        "current_state": "candidate reads as current state",
        "interval_state": "candidate describes a time interval",
        "stale_observation": "time-bound observation; verify whether still current",
        "event": "historical event",
        "atemporal_claim": "durable claim",
    }
    return labels.get(temporal_scope, "")


def orientation_title(entity_label: str, section_hint: str) -> str:
    parts = [part for part in [entity_label, section_hint] if part]
    return " / ".join(parts)


def fact_entity_label(entity_key: Any, page_hint: Any) -> str:
    key_parts = [part for part in str(entity_key or "").split(":") if part]
    useful_parts = [
        part
        for part in key_parts
        if part
        not in {
            "summary",
            "overview",
            "details",
            "concepts",
            "people",
            "projects",
            "career",
        }
    ]
    if useful_parts:
        return humanize_slug(useful_parts[0])
    path = str(page_hint or "").removesuffix(".md")
    if path:
        return humanize_slug(path.split("/")[-1])
    return ""


def humanize_slug(value: str) -> str:
    words = re.sub(r"[_/-]+", " ", value).strip()
    return " ".join(word.capitalize() for word in words.split())


def newest_timestamp(values: list[Any]) -> str:
    timestamps = [str(value) for value in values if str(value or "").strip()]
    return max(timestamps) if timestamps else ""


def first_nonempty(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


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


def is_alternative_fact_comparison(question: dict[str, Any]) -> bool:
    if question.get("kind") != "conflict":
        return False
    return not any(
        isinstance(option, dict) and option.get("option_type") == "candidate_fact"
        for option in question.get("options") or []
    )


def question_alternative_facts(
    context: BrainPaths | QueueBuildContext, question: dict[str, Any]
) -> list[dict[str, Any]]:
    options = [
        option for option in question.get("options") or [] if isinstance(option, dict)
    ]
    option_by_id = {
        str(option.get("id") or option.get("fact_id")): option
        for option in options
        if str(option.get("id") or option.get("fact_id") or "").strip()
    }
    fact_ids = stable_unique_strings(
        [
            *[option.get("id") or option.get("fact_id") for option in options],
            *(question.get("fact_ids") or []),
        ]
    )
    canonical = facts_by_id(context, fact_ids)
    alternatives: list[dict[str, Any]] = []
    for fact_id in fact_ids:
        fact = merge_nonempty(canonical.get(fact_id, {}), option_by_id.get(fact_id, {}))
        if fact:
            alternatives.append(enrich_fact_like(context, fact))
    return alternatives


def question_counterpart_facts(
    context: BrainPaths | QueueBuildContext, question: dict[str, Any]
) -> list[dict[str, Any]]:
    counterparts: list[dict[str, Any]] = []
    for option in question.get("options") or []:
        if isinstance(option, dict) and option.get("option_type") == "existing_fact":
            counterparts.append(dict(option))
    question_context = question.get("context") or {}
    fact_ids = [
        str(fact_id)
        for fact_id in question_context.get("counterpart_fact_ids") or []
        if str(fact_id or "").strip()
    ]
    if not fact_ids and question.get("kind") == "conflict":
        fact_ids = [
            str(fact_id) for fact_id in question.get("fact_ids") or [] if fact_id
        ]
    if fact_ids:
        canonical = facts_by_id(context, fact_ids)
        hydrated: list[dict[str, Any]] = []
        known: set[str] = set()
        for counterpart in counterparts:
            fact_id = str(counterpart.get("id") or counterpart.get("fact_id") or "")
            base = canonical.get(fact_id, {})
            hydrated.append(
                enrich_fact_like(context, merge_nonempty(base, counterpart))
            )
            if fact_id:
                known.add(fact_id)
        for fact_id, fact in canonical.items():
            if fact_id not in known:
                hydrated.append(enrich_fact_like(context, fact))
        return hydrated
    return [enrich_fact_like(context, counterpart) for counterpart in counterparts]


def merge_nonempty(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in overlay.items():
        if value not in (None, "", [], {}):
            merged[key] = value
    return merged


def queue_item_for_action(
    context: BrainPaths | QueueBuildContext, action: dict[str, Any]
) -> dict[str, Any]:
    payload = (action.get("evidence_json") or {}).get("payload")
    topology = action_topology(context, action, payload)
    title = action_title(context, action, payload)
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
        "topology": topology,
        "action": action,
        "proposal": payload,
        "raw": action,
    }


def queue_item_for_audit_action(
    context: BrainPaths | QueueBuildContext, action: dict[str, Any]
) -> dict[str, Any]:
    payload = action_payload(action)
    topology = action_topology(context, action, payload)
    audit = audit_action_detail(context, action, latest_action_audit(action))
    raw_fact = payload.get("fact") if isinstance(payload.get("fact"), dict) else None
    current_fact = None
    if raw_fact:
        candidate_ids = [
            str(fact_id)
            for fact_id in [raw_fact.get("id"), *(action.get("target_fact_ids") or [])]
            if str(fact_id or "").strip()
        ]
        current_facts = facts_by_id(context, list(dict.fromkeys(candidate_ids)))
        statement = " ".join(str(raw_fact.get("statement") or "").split())
        current_fact = next(
            (
                fact
                for fact in current_facts.values()
                if " ".join(str(fact.get("statement") or "").split()) == statement
            ),
            None,
        )
    candidate = (
        enrich_fact_like(context, current_fact or raw_fact)
        if current_fact or raw_fact
        else None
    )
    action_type = str(action.get("action_type") or "action")
    statement = str((candidate or {}).get("statement") or "").strip()
    title = (
        f"Audit finding: {compact_text(statement, 96)}"
        if statement
        else audit_action_title(action_type, topology)
    )
    return {
        "id": action["id"],
        "source_type": "audit",
        "kind": "audit_flagged",
        "group": "audit",
        "title": title,
        "summary": compact_text(audit.get("rationale"), 500),
        "created_at": action.get("applied_at") or action.get("created_at"),
        "status": action.get("status"),
        "risk_tier": action.get("risk_tier"),
        "candidate": candidate,
        "audit": audit,
        "topology": topology,
        "action": action,
        "proposal": payload,
        "raw": action,
    }


def audit_action_title(
    action_type: str, topology: dict[str, Any] | None
) -> str:
    if action_type in {"entity_merge", "page_merge"} and topology:
        destination = str(topology.get("merge_destination_label") or "").strip()
        sources = [
            str(value)
            for value in topology.get("merge_source_labels") or []
            if str(value).strip()
        ]
        if destination and sources:
            return f"Audit finding: Merge {compact_text(', '.join(sources), 48)} into {compact_text(destination, 48)}"
    return f"Audit finding: {humanize_slug(action_type)}"


def audit_action_detail(
    context: BrainPaths | QueueBuildContext,
    action: dict[str, Any],
    audit: dict[str, Any],
) -> dict[str, Any]:
    if isinstance(context, QueueBuildContext):
        reviewability = audit_action_reviewability(context.conn, action)
    else:
        with connection(context.sqlite_path) as conn:
            reviewability = audit_action_reviewability(conn, action)
    fact_ids = [
        str(fact_id)
        for fact_id in action.get("target_fact_ids") or []
        if str(fact_id).strip()
    ]
    if action.get("action_type") == "fact_upsert" and reviewability.get("fact_id"):
        fact_ids = [str(reviewability["fact_id"])]
    inverse = (
        action.get("inverse_action_json")
        if isinstance(action.get("inverse_action_json"), dict)
        else {}
    )
    if not fact_ids:
        inverse_facts: list[dict[str, Any]] = []
        for key in ("restore_facts", "restore_fact"):
            value = inverse.get(key)
            if isinstance(value, dict):
                inverse_facts.append(value)
            elif isinstance(value, list):
                inverse_facts.extend(item for item in value if isinstance(item, dict))
        fact_ids = [
            str(fact.get("id"))
            for fact in inverse_facts
            if str(fact.get("id") or "").strip()
        ]
    fact_ids = list(dict.fromkeys(fact_ids))
    fact_rows = facts_by_id(context, fact_ids[:3])
    affected_facts = [
        enrich_fact_like(context, fact_rows[fact_id])
        for fact_id in fact_ids[:3]
        if fact_id in fact_rows
    ]
    return {
        **audit,
        "affected_fact_count": len(fact_ids),
        "affected_page_count": len(action.get("target_page_paths") or []),
        "affected_contract_count": len(action.get("target_contract_ids") or []),
        "affected_facts": affected_facts,
        "revertible": bool(reviewability["revertible"]),
        "revert_mode": reviewability.get("revert_mode"),
        "reviewability_reason": reviewability["reason"],
    }


def latest_action_audit(action: dict[str, Any]) -> dict[str, Any]:
    raw_evidence = action.get("evidence_json") or {}
    evidence = raw_evidence if isinstance(raw_evidence, dict) else {}
    records = evidence.get("audits")
    valid = [record for record in records or [] if isinstance(record, dict)]
    record = valid[-1] if valid else {}
    metadata = (
        record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
    )
    legacy = evidence.get("audit") if isinstance(evidence.get("audit"), dict) else {}
    rationale = first_nonempty(
        metadata.get("rationale"),
        metadata.get("reason"),
        legacy.get("rationale"),
        evidence.get("reason"),
        action.get("audit_status"),
    )
    return {
        "status": record.get("status") or action.get("audit_status"),
        "rationale": rationale,
        "provider": metadata.get("provider"),
        "model": metadata.get("model"),
        "audited_at": record.get("at"),
        "action_type": action.get("action_type"),
        "action_status": action.get("status"),
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


def action_title(
    context: BrainPaths | QueueBuildContext, action: dict[str, Any], payload: Any
) -> str:
    action_type = str(action.get("action_type") or "action")
    if isinstance(payload, dict):
        if action_type == "entity_merge":
            ids = [
                payload.get("canonical_entity_id"),
                *(payload.get("merged_entity_ids") or []),
            ]
            surfaces = entity_surfaces(context, ids, payload.get("entity_names"))
            if len(surfaces) >= 2:
                return f"Merge {', '.join(surfaces[1:])} into {surfaces[0]}"
            return f"Merge entities: {', '.join(surfaces)}"
        if payload.get("candidate") and isinstance(payload["candidate"], dict):
            return action_title(context, action, payload["candidate"])
        page_hints = topology_page_hints(payload)
        if action_type == "page_split" and page_hints:
            return f"Split page: {page_hints[0]}"
        if action_type == "page_merge" and page_hints:
            return f"Merge pages: {' + '.join(page_hints)}"
        if page_hints:
            return f"{humanize_slug(action_type)}: {', '.join(page_hints)}"
        for key in ("page_hint", "destination_page_hint", "target_path"):
            if payload.get(key):
                return f"{action_type}: {payload[key]}"
    return f"{action_type} · {short_id(str(action.get('id') or ''))}"


def action_topology(
    context: BrainPaths | QueueBuildContext, action: dict[str, Any], payload: Any
) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    candidate = payload.get("candidate")
    if isinstance(candidate, dict):
        payload = candidate
    action_type = str(action.get("action_type") or "action")
    entity_ids = topology_entity_ids(action_type, payload)
    page_hints = topology_page_hints(payload) or [
        str(page_hint)
        for page_hint in action.get("target_page_paths") or []
        if str(page_hint).strip()
    ]
    labels = entity_surfaces(context, entity_ids, payload.get("entity_names"))
    entity_rows = entities_by_id(context, entity_ids)
    entity_statuses = {
        entity_id: str(entity_rows.get(entity_id, {}).get("status") or "")
        for entity_id in entity_ids
    }
    page_statuses = action_page_contract_statuses(context, action, page_hints)
    fallback_label = topology_fallback_label(payload)
    if not labels and fallback_label:
        labels = [fallback_label]
    if not entity_ids and not labels and not page_hints:
        return None
    topology = {
        "entity_ids": entity_ids,
        "entity_labels": labels,
        "entity_statuses": entity_statuses,
        "page_hints": page_hints,
        "page_statuses": page_statuses,
        "target_label": ", ".join(labels) if labels else "",
    }
    if action_type == "entity_merge" and len(labels) >= 2:
        topology["merge_destination_label"] = labels[0]
        topology["merge_source_labels"] = labels[1:]
        topology["target_label"] = f"{', '.join(labels[1:])} into {labels[0]}"
    if action_type == "page_merge" and len(page_hints) >= 2:
        destination, sources = page_merge_direction(
            context, action, payload, page_hints
        )
        if destination and sources:
            topology["merge_destination_label"] = destination
            topology["merge_source_labels"] = sources
            topology["target_label"] = f"{', '.join(sources)} into {destination}"
    if action_type == "page_split" and page_hints:
        topology["split_preview"] = page_split_preview(context, page_hints[0])
    return topology


def page_merge_direction(
    context: BrainPaths | QueueBuildContext,
    action: dict[str, Any],
    payload: dict[str, Any],
    page_hints: list[str],
) -> tuple[str | None, list[str]]:
    destination = str(payload.get("destination_page_hint") or "").strip()
    fact_ids = [
        str(fact_id)
        for fact_id in action.get("target_fact_ids") or []
        if str(fact_id).strip()
    ]
    if not destination and fact_ids:
        current_facts = facts_by_id(context, fact_ids)
        current_pages = {
            str(fact.get("page_hint") or "").strip()
            for fact in current_facts.values()
            if str(fact.get("page_hint") or "").strip() in page_hints
        }
        if len(current_pages) == 1:
            destination = next(iter(current_pages))
    if not destination:
        page_statuses = action_page_contract_statuses(
            context, action, page_hints
        )
        active_pages = [
            page for page in page_hints if page_statuses.get(page) == "active"
        ]
        if len(active_pages) == 1:
            destination = active_pages[0]
    inverse = (
        action.get("inverse_action_json")
        if isinstance(action.get("inverse_action_json"), dict)
        else {}
    )
    restore_facts = inverse.get("restore_facts")
    restored_source_pages = {
        str(fact.get("page_hint") or "").strip()
        for fact in restore_facts or []
        if isinstance(fact, dict) and str(fact.get("page_hint") or "").strip()
    }
    if not destination and restored_source_pages:
        remaining = [page for page in page_hints if page not in restored_source_pages]
        if len(remaining) == 1:
            destination = remaining[0]
    sources = [page for page in page_hints if page != destination]
    return (destination or None), sources


def action_page_contract_statuses(
    context: BrainPaths | QueueBuildContext,
    action: dict[str, Any],
    page_hints: list[str],
) -> dict[str, str]:
    contract_ids = [
        str(contract_id)
        for contract_id in action.get("target_contract_ids") or []
        if str(contract_id).strip()
    ]
    if not contract_ids and not page_hints:
        return {}
    if contract_ids:
        placeholders = ",".join("?" for _ in contract_ids)
        query = f"SELECT page_hint, status, updated_at, id FROM page_contracts WHERE id IN ({placeholders}) ORDER BY updated_at DESC, id DESC"
        params = contract_ids
    else:
        placeholders = ",".join("?" for _ in page_hints)
        query = f"SELECT page_hint, status, updated_at, id FROM page_contracts WHERE page_hint IN ({placeholders}) ORDER BY updated_at DESC, id DESC"
        params = page_hints
    if isinstance(context, QueueBuildContext):
        rows = context.conn.execute(query, params)
        return first_page_contract_statuses(rows, page_hints)
    with connection(context.sqlite_path) as conn:
        return first_page_contract_statuses(conn.execute(query, params), page_hints)


def first_page_contract_statuses(
    rows: Any, page_hints: list[str]
) -> dict[str, str]:
    allowed = set(page_hints)
    statuses: dict[str, str] = {}
    for row in rows:
        page_hint = str(row["page_hint"] or "")
        if page_hint in allowed and page_hint not in statuses:
            statuses[page_hint] = str(row["status"] or "")
    return statuses


def topology_entity_ids(action_type: str, payload: dict[str, Any]) -> list[str]:
    ids: list[str] = []
    if action_type == "entity_merge":
        ids.append(str(payload.get("canonical_entity_id") or "").strip())
        ids.extend(
            str(value or "").strip() for value in payload.get("merged_entity_ids") or []
        )
    for key in ("entity_id", "canonical_entity_id", "canonical_entity"):
        ids.append(str(payload.get(key) or "").strip())
    contract = payload.get("contract")
    if isinstance(contract, dict):
        ids.append(str(contract.get("canonical_entity") or "").strip())
    seen: set[str] = set()
    unique = []
    for entity_id in ids:
        if entity_id and entity_id not in seen:
            seen.add(entity_id)
            unique.append(entity_id)
    return unique


def topology_page_hints(payload: dict[str, Any]) -> list[str]:
    values = [
        payload.get("page_hint"),
        payload.get("destination_page_hint"),
        payload.get("target_path"),
    ]
    contract = payload.get("contract")
    if isinstance(contract, dict):
        values.append(contract.get("page_hint"))
    values.extend(payload.get("page_hints") or [])
    seen: set[str] = set()
    hints = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in seen:
            seen.add(text)
            hints.append(text)
    return hints


def topology_fallback_label(payload: dict[str, Any]) -> str:
    contract = payload.get("contract")
    if isinstance(contract, dict):
        return humanize_entity_id(str(contract.get("canonical_entity") or ""))
    return humanize_entity_id(
        str(payload.get("canonical_entity") or payload.get("entity_id") or "")
    )


def entity_surfaces(
    context: BrainPaths | QueueBuildContext,
    entity_ids: list[Any],
    embedded_names: Any = None,
) -> list[str]:
    ids = [
        str(entity_id or "").strip()
        for entity_id in entity_ids
        if str(entity_id or "").strip()
    ]
    names = embedded_names if isinstance(embedded_names, dict) else {}
    rows = entities_by_id(context, ids)
    surfaces = []
    for entity_id in ids:
        surface = str(
            names.get(entity_id) or rows.get(entity_id, {}).get("name") or ""
        ).strip()
        surfaces.append(surface or humanize_entity_id(entity_id) or entity_id)
    return surfaces


def humanize_entity_id(entity_id: str) -> str:
    value = entity_id.strip()
    if not value:
        return ""
    if value.startswith("entity_"):
        return ""
    if ":" in value:
        value = value.split(":")[-1]
    return humanize_slug(value)


def action_summary(action: dict[str, Any], payload: Any) -> str:
    if isinstance(payload, dict):
        candidate = payload.get("candidate")
        if isinstance(candidate, dict):
            return action_summary(action, candidate)
        return compact_text(
            payload.get("reason")
            or payload.get("candidate_signal")
            or json.dumps(payload, sort_keys=True),
            240,
        )
    return compact_text((action.get("evidence_json") or {}).get("reason"), 240)


def page_split_preview(
    context: BrainPaths | QueueBuildContext, page_hint: str
) -> dict[str, Any]:
    if isinstance(context, QueueBuildContext):
        rows = list(
            context.conn.execute(
                """
                SELECT *
                FROM facts
                WHERE page_hint = ? AND status = 'active'
                ORDER BY section_hint, created_at, id
                """,
                (page_hint,),
            )
        )
    else:
        with connection(context.sqlite_path) as conn:
            rows = list(
                conn.execute(
                    """
                    SELECT *
                    FROM facts
                    WHERE page_hint = ? AND status = 'active'
                    ORDER BY section_hint, created_at, id
                    """,
                    (page_hint,),
                )
            )
    sections: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        fact = row_to_fact(row)
        section = str(fact.get("section_hint") or "").strip()
        if section.lower() in {"", "summary", "unsectioned"}:
            continue
        sections.setdefault(section, []).append(fact)
    children = []
    for section, facts in sorted(sections.items()):
        children.append(
            {
                "section": section,
                "page_hint": split_destination_page_hint(page_hint, section),
                "fact_count": len(facts),
                "representative_facts": [
                    {
                        "id": fact.get("id"),
                        "statement": fact.get("statement"),
                        "evidence_quote": fact.get("evidence_quote"),
                    }
                    for fact in facts[:3]
                ],
            }
        )
    return {
        "source_page_hint": page_hint,
        "source_page_retained": True,
        "resulting_page_count": len(children) + 1,
        "movable_fact_count": sum(child["fact_count"] for child in children),
        "children": children,
        "approvable": bool(children),
    }


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
    source_references = fact_source_references(output)
    resolved_documents = queue_source_document_summaries(context, source_references)
    output["source_documents"] = merge_source_documents(
        resolved_documents, output.get("source_documents")
    )
    output["source_date"], output["source_date_basis"] = derive_fact_source_date(
        output, output["source_documents"]
    )
    fact_id = str(output.get("id") or output.get("fact_id") or "").strip()
    if (
        isinstance(context, QueueBuildContext)
        and context.include_popularity
        and fact_id
    ):
        popularity = context.fact_popularity([fact_id])
        output["retrieval_count"] = popularity["retrieval_count"]
        output["last_retrieved_at"] = popularity["last_retrieved_at"]
    return output


def fact_source_references(fact: dict[str, Any]) -> list[str]:
    references = [str(source_id) for source_id in fact.get("source_ids") or []]
    source_spans = fact.get("source_spans") or []
    if isinstance(source_spans, str):
        source_spans = json_loads(source_spans, [])
    for span in source_spans if isinstance(source_spans, list) else []:
        if not isinstance(span, dict):
            continue
        source_id = str(span.get("source_id") or "").strip()
        chunk_id = str(span.get("chunk_id") or "").strip()
        if source_id:
            references.append(source_id)
        if chunk_id:
            references.append(
                chunk_id if chunk_id.startswith("chunk:") else f"chunk:{chunk_id}"
            )
    return list(dict.fromkeys(reference for reference in references if reference))


def merge_source_documents(
    resolved: list[dict[str, Any]], embedded: Any
) -> list[dict[str, Any]]:
    documents = [dict(document) for document in resolved]
    positions = {
        str(document.get("source_id") or document.get("id") or ""): index
        for index, document in enumerate(documents)
    }
    for raw in embedded if isinstance(embedded, list) else []:
        if not isinstance(raw, dict):
            continue
        document = dict(raw)
        key = str(document.get("source_id") or document.get("id") or "")
        if key and key in positions:
            documents[positions[key]] = merge_nonempty(
                documents[positions[key]], document
            )
        else:
            documents.append(document)
            if key:
                positions[key] = len(documents) - 1
    return documents


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


def entities_by_id(
    context: BrainPaths | QueueBuildContext, entity_ids: list[str]
) -> dict[str, dict[str, Any]]:
    if not entity_ids:
        return {}
    if isinstance(context, QueueBuildContext):
        return context.entities(entity_ids)
    placeholders = ",".join("?" for _ in entity_ids)
    paths = queue_context_paths(context)
    with connection(paths.sqlite_path) as conn:
        if not ui_table_exists(conn, "entities"):
            return {}
        rows = conn.execute(
            f"""
            SELECT id, name, entity_type, status
            FROM entities
            WHERE id IN ({placeholders})
            """,
            entity_ids,
        )
        return {str(row["id"]): dict(row) for row in rows}


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
    terms = {
        term.casefold() for term in query.replace("-", " ").split() if len(term) > 2
    }
    document_id = route_candidate_document_id(context, fact)
    if isinstance(context, QueueBuildContext):
        priors = context.document_route_priors(document_id)
    else:
        with connection(context.sqlite_path) as conn:
            priors = load_document_route_priors(conn, document_id)
    priors_by_page = {
        str(prior.get("page_hint") or ""): prior
        for prior in priors
        if prior.get("page_hint")
    }
    scored: list[tuple[float, int, dict[str, Any], dict[str, Any]]] = []
    for page in pages:
        if not is_routable_wiki_page(
            page_type=page.get("page_type"),
            relative_path=page.get("relative_path"),
            title=page.get("title"),
        ):
            continue
        haystack = " ".join(
            [str(page.get("title") or ""), str(page.get("relative_path") or "")]
        ).casefold()
        lexical_score = sum(1 for term in terms if term in haystack)
        prior = priors_by_page.get(str(page.get("relative_path") or "")) or {}
        score = float(lexical_score) + coherence_bonus(prior)
        if page.get("relative_path") == page_hint:
            score += 10
        if score:
            scored.append((score, int(prior.get("fact_count") or 0), page, prior))
    scored.sort(
        key=lambda item: (
            -item[0],
            -item[1],
            item[2].get("relative_path") or "",
        )
    )
    return [
        {
            "page_hint": page["relative_path"],
            "title": compact_text(page["title"], 120),
            "score": round(score, 4),
            "page_type": page.get("page_type"),
            "document_coherence_count": int(prior.get("fact_count") or 0),
            "document_coherence_share": float(prior.get("share") or 0.0),
        }
        for score, _count, page, prior in scored[:5]
    ]


def route_candidate_document_id(
    context: BrainPaths | QueueBuildContext, fact: dict[str, Any]
) -> str:
    document_id = fact_document_id(fact)
    if document_id:
        return document_id
    documents = queue_source_document_summaries(context, fact.get("source_ids") or [])
    return str(documents[0].get("id") or "") if len(documents) == 1 else ""


def queue_active_pages(paths: BrainPaths, conn: Any) -> list[dict[str, Any]]:
    if not ui_table_exists(conn, "wiki_pages"):
        return []
    pages = []
    non_routable_types = sorted(NON_ROUTABLE_PAGE_TYPES)
    placeholders = ", ".join("?" for _ in non_routable_types)
    rows = conn.execute(
        f"""
        SELECT title, page_type, path
        FROM wiki_pages
        WHERE status = 'active'
          AND lower(page_type) NOT IN ({placeholders})
        ORDER BY page_type, title
        """,
        non_routable_types,
    )
    wiki_root = paths.wiki.resolve()
    for row in rows:
        try:
            target = Path(str(row["path"]))
            relative_path = target.resolve().relative_to(wiki_root).as_posix()
            safe_wiki_path(paths, relative_path, must_exist=False)
        except (ValueError, BadRequestError, NotFoundError, OSError):
            continue
        page = {
            "title": row["title"],
            "relative_path": relative_path,
            "page_type": row["page_type"],
        }
        if is_routable_wiki_page(**page):
            pages.append(page)
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
    if limit <= 3:
        return text[: max(0, limit)]
    return f"{text[: limit - 3].rstrip()}..."


def short_id(value: str) -> str:
    return value[:10]


def bounded_int(value: str | None, *, default: int, minimum: int, maximum: int) -> int:
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
    normalized_decision = decision.replace("-", "_")
    if normalized_decision not in {"skip", "unsure", "skip_later", "escalate"}:
        card = queue_card_for_target(paths, existing)
        if card.get("approvable") is not True:
            reason = str(
                card.get("blocking_reason") or "The review card is incomplete."
            )
            raise BadRequestError(f"Review item is not approvable: {reason}")
    source_type = existing["source_type"]
    if source_type == "question":
        result = decide_queue_question(paths, existing["item"], decision, payload)
    elif source_type == "memory":
        result = decide_queue_memory(paths, existing["item"], decision, payload)
    elif source_type == "action":
        result = decide_queue_action(paths, existing["item"], decision, payload)
    elif source_type == "audit":
        result = decide_queue_audit(paths, existing["item"], decision, payload)
    else:
        raise NotFoundError(f"queue item not found: {item_id}")
    result["queue_summary"] = current_queue_summary(paths)
    return result


def queue_card_for_target(paths: BrainPaths, target: dict[str, Any]) -> dict[str, Any]:
    with connection(paths.sqlite_path) as conn:
        ctx = QueueBuildContext(paths, conn, include_popularity=False)
        descriptor = {
            "source_type": target["source_type"],
            "item": target["item"],
        }
        return queue_item_from_descriptor(
            ctx, descriptor, include_route_candidates=False
        )


def find_queue_target(paths: BrainPaths, item_id: str) -> dict[str, Any]:
    with connection(paths.sqlite_path) as conn:
        if ui_table_exists(conn, "open_questions"):
            row = conn.execute(
                "SELECT * FROM open_questions WHERE id = ?", (item_id,)
            ).fetchone()
            if row:
                return {"source_type": "question", "item": row_to_question(row)}
        if ui_table_exists(conn, "memories"):
            row = conn.execute(
                "SELECT * FROM memories WHERE id = ?", (item_id,)
            ).fetchone()
            if row:
                return {
                    "source_type": "memory",
                    "item": service(paths).get_memory(item_id),
                }
        if ui_table_exists(conn, "cos_actions"):
            row = conn.execute(
                "SELECT * FROM cos_actions WHERE id = ?", (item_id,)
            ).fetchone()
            if row:
                action = row_to_action(row)
                if action.get("audit_status") == "sampled_bad":
                    if not audit_action_reviewability(conn, action)["reviewable"]:
                        raise NotFoundError(f"queue item not found: {item_id}")
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
    if question.get("kind") == "unrouted_inbox_batch":
        return decide_unrouted_inbox_batch(paths, question, normalized, payload)
    if question.get("kind") == "document_extraction_anomaly":
        return decide_extraction_anomaly(paths, question, normalized, payload)
    alternative_comparison = is_alternative_fact_comparison(question)
    if alternative_comparison and normalized not in {
        "skip",
        "escalate",
        "unsure",
        "skip_later",
        "both_true",
        "contested",
        "select_facts",
        "select_fact",
        "keep_fact",
    }:
        raise BadRequestError(
            "historical comparisons require selecting one or more facts to keep"
        )
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
    if normalized in {"unsure", "skip_later"}:
        return {
            "status": "skipped",
            "item_id": question["id"],
            "result": {"question": question},
            "undo_handle": None,
        }
    if alternative_comparison and normalized in {
        "both_true",
        "contested",
        "select_facts",
        "select_fact",
        "keep_fact",
    }:
        if normalized in {"both_true", "contested"}:
            selected_fact_ids = [
                str(fact_id)
                for fact_id in question.get("fact_ids") or []
                if str(fact_id or "").strip()
            ]
        elif normalized in {"select_fact", "keep_fact"}:
            selected_fact_id = str(payload.get("selected_fact_id") or "").strip()
            if not selected_fact_id:
                raise BadRequestError("selected_fact_id is required")
            selected_fact_ids = [selected_fact_id]
        else:
            raw_selected = payload.get("selected_fact_ids")
            if not isinstance(raw_selected, list):
                raise BadRequestError("selected_fact_ids must be an array")
            selected_fact_ids = [
                str(fact_id).strip()
                for fact_id in raw_selected
                if str(fact_id or "").strip()
            ]
            if not selected_fact_ids:
                raise BadRequestError(
                    "selected_fact_ids must contain at least one fact"
                )
        action_states = linked_question_action_states(paths, question)
        result = ui_answer_wiki_question(
            paths, question["id"], {"selected_fact_ids": selected_fact_ids}
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
    if normalized in {"both_true", "contested"}:
        return contest_question_candidate(paths, question, previous)
    if normalized in {"supports", "supports_existing", "merge_evidence"}:
        return support_existing_question_candidate(paths, question, previous)
    if normalized in {"temporal_update", "updates", "current_state"}:
        return temporal_update_question_candidate(paths, question, previous)
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
                "action": action_undo_state(
                    paths, str(question.get("action_id") or "")
                ),
            },
        }
    raise BadRequestError(f"unsupported queue decision: {decision}")


def decide_extraction_anomaly(
    paths: BrainPaths,
    question: dict[str, Any],
    decision: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    if decision in {"skip", "unsure", "skip_later"}:
        return {
            "status": "skipped",
            "item_id": question["id"],
            "result": {"question": question},
            "undo_handle": None,
        }
    if decision in {"approve", "accept", "acknowledge", "reviewed"}:
        status = "answered"
        answer_decision = "acknowledged"
    elif decision in {"reject", "dismiss"}:
        status = "dismissed"
        answer_decision = "dismissed"
    else:
        raise BadRequestError(f"unsupported extraction alert decision: {decision}")
    previous = question_undo_state(question)
    anomaly = extraction_anomaly_summary(question)
    mark_review_question_decided(
        paths,
        question["id"],
        status=status,
        answer={
            "decision": answer_decision,
            "document_id": anomaly.get("document_id"),
            "block_rate": anomaly.get("block_rate"),
            "note": str(payload.get("note") or "").strip(),
        },
        action_id=None,
    )
    return {
        "status": "decided",
        "item_id": question["id"],
        "result": {"question": get_review_question(paths, question["id"])},
        "undo_handle": {
            "kind": "question_reject",
            "question": previous,
            "action": {},
        },
    }


def decide_unrouted_inbox_batch(
    paths: BrainPaths,
    question: dict[str, Any],
    decision: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    if decision in {"skip", "unsure", "skip_later"}:
        return {
            "status": "skipped",
            "item_id": question["id"],
            "result": {"question": question},
            "undo_handle": None,
        }
    if decision not in {"approve", "accept", "reviewed", "reject", "dismiss"}:
        raise BadRequestError(f"unsupported Inbox batch decision: {decision}")
    previous = question_undo_state(question)
    accepted = decision in {"approve", "accept", "reviewed"}
    resolved_status = "answered" if accepted else "dismissed"
    answer = {
        "decision": "reviewed" if accepted else "dismiss",
        "reason": str(payload.get("reason") or "").strip(),
        "page_hint": question.get("page_hint"),
    }
    mark_review_question_decided(
        paths,
        question["id"],
        status=resolved_status,
        answer=answer,
        action_id=None,
    )
    return {
        "status": "decided",
        "item_id": question["id"],
        "result": {"question": get_review_question(paths, question["id"])},
        "undo_handle": {
            "kind": "question_reject",
            "question": previous,
            "action": {},
        },
    }


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


def support_existing_question_candidate(
    paths: BrainPaths, question: dict[str, Any], previous: dict[str, Any]
) -> dict[str, Any]:
    candidate = question_candidate_fact(paths, question)
    if not candidate:
        raise BadRequestError("supports_existing requires a candidate fact")
    counterpart_id = first_counterpart_fact_id(paths, question)
    if not counterpart_id:
        raise BadRequestError("supports_existing requires an existing fact")
    existing = facts_by_id(paths, [counterpart_id]).get(counterpart_id)
    if not existing:
        raise BadRequestError(f"existing fact not found: {counterpart_id}")
    action_states = linked_question_action_states(paths, question)
    merged = supported_existing_fact(existing, candidate, question["id"])
    action = apply_action(
        paths,
        propose_action(
            paths,
            "fact_upsert",
            action_payload={"fact": merged},
            action_features={
                "human_confirmed": True,
                "truth_mutation": False,
                "reversible": True,
                "affected_fact_count": 1,
                "relation": "supports",
            },
            target_fact_ids=[counterpart_id],
            target_page_paths=[str(merged.get("page_hint") or "")]
            if merged.get("page_hint")
            else [],
            proposed_by="ui_queue_supports_existing",
            confidence=float(merged.get("confidence") or 1.0),
            risk_tier="low",
        )["id"],
    )
    old_action_id = str(question.get("action_id") or "").strip()
    if old_action_id:
        reject_linked_review_action(
            paths, old_action_id, "replaced by supports-existing queue decision"
        )
    mark_review_question_decided(
        paths,
        question["id"],
        status="answered",
        answer={
            "decision": "supports_existing",
            "existing_fact_id": counterpart_id,
            "support_action_id": action["id"],
        },
        action_id=action["id"],
    )
    return {
        "status": "decided",
        "item_id": question["id"],
        "result": {
            "question": get_review_question(paths, question["id"]),
            "action": action,
        },
        "undo_handle": {
            "kind": "question_actions",
            "question": previous,
            "action_ids": [action["id"]],
            "actions": action_states,
        },
    }


def temporal_update_question_candidate(
    paths: BrainPaths, question: dict[str, Any], previous: dict[str, Any]
) -> dict[str, Any]:
    candidate = question_candidate_fact(paths, question)
    if not candidate:
        raise BadRequestError("temporal_update requires a candidate fact")
    counterpart_ids = [
        str(fact.get("id") or fact.get("fact_id"))
        for fact in question_counterpart_facts(paths, question)
        if fact.get("id") or fact.get("fact_id")
    ]
    if not counterpart_ids:
        raise BadRequestError("temporal_update requires existing fact ids")
    action_states = linked_question_action_states(paths, question)
    temporal_candidate = temporal_update_fact(
        candidate, counterpart_ids, question["id"]
    )
    candidate_action = apply_action(
        paths,
        propose_action(
            paths,
            "fact_upsert",
            action_payload={"fact": temporal_candidate},
            action_features={
                "human_confirmed": True,
                "truth_mutation": True,
                "reversible": True,
                "affected_fact_count": 1 + len(counterpart_ids),
                "relation": "updates",
            },
            target_fact_ids=[str(temporal_candidate["id"])],
            target_page_paths=[str(temporal_candidate.get("page_hint") or "")]
            if temporal_candidate.get("page_hint")
            else [],
            proposed_by="ui_queue_temporal_update",
            confidence=float(temporal_candidate.get("confidence") or 1.0),
            risk_tier="medium",
        )["id"],
    )
    supersede_action = apply_fact_status_action(
        paths,
        "fact_supersede",
        [
            {"fact_id": fact_id, "status": "superseded", "conflict_group_id": None}
            for fact_id in counterpart_ids
        ],
        proposed_by="ui_queue_temporal_update",
        risk_tier="medium",
    )
    old_action_id = str(question.get("action_id") or "").strip()
    if old_action_id:
        reject_linked_review_action(
            paths, old_action_id, "replaced by temporal-update queue decision"
        )
    action_ids = [candidate_action["id"]]
    actions = [candidate_action]
    if supersede_action.get("id"):
        action_ids.insert(0, str(supersede_action["id"]))
        actions.append(supersede_action)
    mark_review_question_decided(
        paths,
        question["id"],
        status="answered",
        answer={
            "decision": "temporal_update",
            "candidate_fact_id": str(temporal_candidate["id"]),
            "superseded_fact_ids": counterpart_ids,
            "candidate_action_id": candidate_action["id"],
            "supersede_action_id": supersede_action.get("id"),
        },
        action_id=candidate_action["id"],
    )
    return {
        "status": "decided",
        "item_id": question["id"],
        "result": {
            "question": get_review_question(paths, question["id"]),
            "actions": actions,
        },
        "undo_handle": {
            "kind": "question_actions",
            "question": previous,
            "action_ids": action_ids,
            "actions": action_states,
        },
    }


def supported_existing_fact(
    existing: dict[str, Any], candidate: dict[str, Any], question_id: str
) -> dict[str, Any]:
    timestamp = now_iso()
    metadata = (
        existing.get("metadata") if isinstance(existing.get("metadata"), dict) else {}
    )
    support_records = metadata.get("supporting_candidates")
    if not isinstance(support_records, list):
        support_records = []
    support_records.append(
        {
            "question_id": question_id,
            "statement": candidate.get("statement"),
            "evidence_quote": candidate.get("evidence_quote") or candidate.get("quote"),
            "source_ids": candidate.get("source_ids") or [],
            "observed_at": candidate.get("observed_at"),
            "attached_at": timestamp,
        }
    )
    source_spans = [
        *(existing.get("source_spans") or []),
        *(candidate.get("source_spans") or []),
    ]
    return {
        **existing,
        "source_ids": stable_unique_strings(
            [*(existing.get("source_ids") or []), *(candidate.get("source_ids") or [])]
        ),
        "source_spans": source_spans,
        "metadata": {**metadata, "supporting_candidates": support_records[-25:]},
        "last_seen_at": candidate.get("observed_at") or timestamp,
    }


def temporal_update_fact(
    candidate: dict[str, Any], counterpart_ids: list[str], question_id: str
) -> dict[str, Any]:
    timestamp = now_iso()
    metadata = (
        candidate.get("metadata") if isinstance(candidate.get("metadata"), dict) else {}
    )
    fact_id = str(candidate.get("id") or new_id("fact"))
    return {
        **candidate,
        "id": fact_id,
        "status": "active",
        "supersedes_id": counterpart_ids[0],
        "metadata": {
            **metadata,
            "temporal_update": {
                "question_id": question_id,
                "superseded_fact_ids": counterpart_ids,
                "decided_at": timestamp,
            },
        },
        "last_seen_at": candidate.get("last_seen_at")
        or candidate.get("observed_at")
        or timestamp,
    }


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
        candidate_fact_ids.extend(
            str(item) for item in applied.get("target_fact_ids") or []
        )
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
        "actions": [
            get_action_for_queue(paths, action_id) for action_id in applied_ids
        ],
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


def first_counterpart_fact_id(
    paths: BrainPaths, question: dict[str, Any]
) -> str | None:
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
        with connection(paths.sqlite_path) as conn:
            reviewability = audit_action_reviewability(conn, action)
        if not reviewability["revertible"]:
            raise BadRequestError(
                "audited action no longer has a safe direct revert; review the current fact or topology state"
            )
        if reviewability.get("revert_mode") == "reject_current_fact":
            fact_id = str(reviewability.get("fact_id") or "")
            correction = apply_fact_status_action(
                paths,
                "fact_supersede",
                [
                    {
                        "fact_id": fact_id,
                        "status": "rejected",
                        "conflict_group_id": None,
                    }
                ],
                proposed_by="ui_audit_reject_current_fact",
                risk_tier="medium",
            )
            if not correction.get("id"):
                raise BadRequestError("audited fact is no longer active")
            result = record_action_audit(
                paths,
                action["id"],
                "remediated",
                metadata={
                    "ui_rejected_current_fact": True,
                    "fact_id": fact_id,
                    "correction_action_id": correction["id"],
                },
            )
            result_payload = {"action": result, "correction_action": correction}
            undo = {
                "kind": "audit_fact_remediation",
                "action": previous,
                "correction_action_id": correction["id"],
            }
        else:
            result = revert_action(paths, action["id"])
            result_payload = {"action": result}
            undo = {"kind": "action_revert", "action": previous}
    elif normalized in {"ok", "mark_ok", "mark_good"}:
        result = record_action_audit(
            paths,
            action["id"],
            "sampled_ok",
            metadata={"ui_marked_ok": True, "note": payload.get("note") or ""},
        )
        result_payload = {"action": result}
        undo = {"kind": "action_status", "action": previous}
    else:
        raise BadRequestError(f"unsupported audit decision: {decision}")
    return {
        "status": "decided",
        "item_id": action["id"],
        "result": result_payload,
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
    elif kind == "audit_fact_remediation":
        correction_action_id = str(handle.get("correction_action_id") or "")
        if correction_action_id:
            revert_action(paths, correction_action_id)
        restore_action_state(paths, handle.get("action") or {})
    elif kind == "action_status":
        restore_action_state(paths, handle.get("action") or {})
    else:
        raise BadRequestError(f"unsupported undo handle: {kind}")
    return {
        "status": "undone",
        "undo_handle": handle,
        "queue_summary": current_queue_summary(paths),
    }


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
    if first(query, "routable") == "1":
        with connection(paths.sqlite_path) as conn:
            pages = queue_active_pages(paths, conn)
        return {"pages": pages, "count": len(pages)}
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
        "snapshots": snapshots_for_page(
            paths, target.relative_to(paths.wiki).as_posix()
        ),
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
    relative_path = str(
        payload.get("path") or payload.get("relative_path") or ""
    ).strip()
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
    sort_mode = normalize_entity_sort(first(query, "sort") or "retrieval")
    order_by = {
        "retrieval": "retrieval_count DESC, last_retrieved_at DESC, fact_count DESC, e.name COLLATE NOCASE",
        "facts": "fact_count DESC, retrieval_count DESC, e.name COLLATE NOCASE",
        "name": "e.name COLLATE NOCASE, retrieval_count DESC",
        "recent": "last_observed_at DESC, retrieval_count DESC, e.name COLLATE NOCASE",
    }[sort_mode]
    with connection(paths.sqlite_path) as conn:
        if not ui_table_exists(conn, "entities"):
            return {"entities": [], "count": 0, "types": [], "sort": sort_mode}
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
                WITH fact_stats AS (
                  SELECT fe.entity_id,
                         COUNT(DISTINCT f.id) AS fact_count,
                         MAX(COALESCE(f.observed_at, f.created_at)) AS last_observed_at
                  FROM fact_entities fe
                  JOIN facts f ON f.id = fe.fact_id
                    AND f.status IN ('active', 'conflicted')
                  GROUP BY fe.entity_id
                ), entity_popularity AS (
                  SELECT fe.entity_id,
                         COUNT(DISTINCT cle.retrieval_event_id) AS retrieval_count,
                         MAX(cle.created_at) AS last_retrieved_at
                  FROM fact_entities fe
                  JOIN context_lineage_events cle
                    ON cle.target_type = 'fact'
                   AND cle.target_id = fe.fact_id
                   AND cle.event_type = 'exposed'
                   AND cle.retrieval_event_id IS NOT NULL
                  GROUP BY fe.entity_id
                )
                SELECT e.*,
                       COALESCE(fs.fact_count, 0) AS fact_count,
                       fs.last_observed_at,
                       COALESCE(ep.retrieval_count, 0) AS retrieval_count,
                       ep.last_retrieved_at
                FROM entities e
                LEFT JOIN fact_stats fs ON fs.entity_id = e.id
                LEFT JOIN entity_popularity ep ON ep.entity_id = e.id
                {where}
                ORDER BY {order_by}
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
            if q
            in " ".join(
                [
                    str(entity.get("name") or ""),
                    str(entity.get("entity_type") or ""),
                    " ".join(entity.get("aliases") or []),
                ]
            ).casefold()
        ]
    return {
        "entities": entities,
        "count": len(entities),
        "types": types,
        "sort": sort_mode,
    }


def normalize_entity_sort(value: str) -> str:
    normalized = str(value or "retrieval").strip().lower()
    aliases = {
        "popular": "retrieval",
        "popularity": "retrieval",
        "fact_count": "facts",
        "newest": "recent",
    }
    normalized = aliases.get(normalized, normalized)
    if normalized not in {"retrieval", "facts", "name", "recent"}:
        raise BadRequestError("sort must be one of: retrieval, facts, name, recent")
    return normalized


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
        "retrieval_count": int(row.get("retrieval_count") or 0),
        "last_retrieved_at": row.get("last_retrieved_at"),
        "last_observed_at": row.get("last_observed_at"),
        "created_at": row.get("created_at"),
    }


def ui_entity_detail(paths: BrainPaths, entity_id: str) -> dict[str, Any]:
    service(paths).init_workspace()
    with connection(paths.sqlite_path) as conn:
        row = conn.execute(
            "SELECT * FROM entities WHERE id = ?", (entity_id,)
        ).fetchone()
        if not row:
            raise NotFoundError(f"entity not found: {entity_id}")
        popularity = popularity_for_ids(entity_retrieval_event_index(conn), [entity_id])
        entity = entity_index_card(
            {
                **row_to_plain_dict(row),
                "fact_count": 0,
                **popularity,
            }
        )
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
        WITH fact_popularity AS (
          SELECT target_id AS fact_id,
                 COUNT(DISTINCT retrieval_event_id) AS retrieval_count,
                 MAX(created_at) AS last_retrieved_at
          FROM context_lineage_events
          WHERE target_type = 'fact'
            AND event_type = 'exposed'
            AND retrieval_event_id IS NOT NULL
          GROUP BY target_id
        )
        SELECT DISTINCT f.*,
               COALESCE(fp.retrieval_count, 0) AS retrieval_count,
               fp.last_retrieved_at
        FROM facts f
        JOIN fact_entities fe ON fe.fact_id = f.id
        LEFT JOIN fact_popularity fp ON fp.fact_id = f.id
        WHERE fe.entity_id = ?
          AND f.status IN ('active', 'conflicted')
        ORDER BY retrieval_count DESC, COALESCE(f.observed_at, f.created_at) DESC
        LIMIT 500
        """,
        (entity_id,),
    )
    facts = []
    for row in rows:
        fact = row_to_fact(row)
        fact["retrieval_count"] = int(row["retrieval_count"] or 0)
        fact["last_retrieved_at"] = row["last_retrieved_at"]
        facts.append(fact)
    return facts


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
            risk_tier=str(
                payload.get("risk_tier") or features.get("risk_tier") or "medium"
            ),
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


def ui_curation_settings(paths: BrainPaths) -> dict[str, Any]:
    service(paths).init_workspace()
    return load_curation_settings(paths)


def ui_update_curation_settings(
    paths: BrainPaths, payload: dict[str, Any]
) -> dict[str, Any]:
    service(paths).init_workspace()
    strictness = optional_str(payload.get("strictness"))
    try:
        return update_curation_settings(
            paths,
            strictness,
            merge_aggressiveness=payload.get("merge_aggressiveness"),
            split_aggressiveness=payload.get("split_aggressiveness"),
            topology_review_threshold=payload.get("topology_review_threshold"),
        )
    except ValueError as exc:
        raise BadRequestError(str(exc)) from exc


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
        "note": COS_AUDIT_CONFIGURED_NOTE
        if has_configured_audits
        else COS_AUDIT_STUB_NOTE,
        "counts": counts,
        "failures": failures,
    }


def ui_generate_cos_contracts(
    paths: BrainPaths, payload: dict[str, Any]
) -> dict[str, Any]:
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
        review = managed_fact_page_review(paths, page_hint)
    except ValueError as exc:
        raise BadRequestError(str(exc)) from exc
    with connection(paths.sqlite_path) as conn:
        context = QueueBuildContext(paths, conn)
        for key in ("facts", "active_facts"):
            values = review.get(key)
            if isinstance(values, list):
                review[key] = [
                    enrich_fact_like(context, fact)
                    for fact in values
                    if isinstance(fact, dict)
                ]
    return review


def ui_answer_wiki_question(
    paths: BrainPaths, question_id: str, payload: dict[str, Any]
) -> dict[str, Any]:
    service(paths).init_workspace()
    raw_selected_fact_ids = payload.get("selected_fact_ids")
    if raw_selected_fact_ids is not None and not isinstance(
        raw_selected_fact_ids, list
    ):
        raise BadRequestError("selected_fact_ids must be an array")
    try:
        return answer_open_question(
            paths,
            question_id,
            selected_fact_id=optional_str(payload.get("selected_fact_id")),
            selected_fact_ids=[
                str(fact_id).strip()
                for fact_id in raw_selected_fact_ids or []
                if str(fact_id or "").strip()
            ]
            if raw_selected_fact_ids is not None
            else None,
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
        retire_open_candidate_siblings(
            conn,
            action,
            reason="candidate rejected by human review",
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
    with connection(paths.sqlite_path) as conn:
        return QueueBuildContext(
            paths, conn, include_popularity=False
        ).source_documents(source_ids)


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
