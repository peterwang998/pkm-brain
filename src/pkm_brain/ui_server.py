from __future__ import annotations

import json
import os
import re
import secrets
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import yaml

from .audit import audit_memories
from .automation import index_status
from .contracts import active_page_contracts, generate_initial_contracts
from .cos_actions import recent_actions
from .cos_audit import run_sampled_audit
from .cos_policy import active_policy_rules, active_policy_version
from .db import connection
from .paths import BrainPaths
from .scheduler.launchd import LaunchdScheduler
from .service import BrainService
from .setup_wizard import build_setup_plan
from .wiki_fact_migration import migrate_existing_wiki_to_facts
from .wiki import (
    ALLOWED_PAGE_TYPES,
    ALLOWED_STATUSES,
    GENERATED_MARKER,
    lint_wiki,
    parse_frontmatter,
)
from .wiki_facts import (
    answer_open_question,
    absorb_wiki_packet_into_facts,
    create_confirmed_page_fact,
    managed_fact_page_review,
    reconcile_open_fact_questions,
    regenerate_managed_fact_page,
    revert_wiki_page_snapshot,
    wiki_fact_dashboard,
)
from .wiki_proposals import (
    PENDING_ITEM_STATUS,
    PENDING_REVIEW_STATUSES,
    append_to_section,
    apply_wiki_proposal,
    generate_interview_questions,
    generate_wiki_review_packet_brief,
    inspect_wiki_proposal,
    list_wiki_review_packets,
    list_wiki_proposals,
    record_wiki_interview,
    reject_wiki_proposal,
    replace_section,
    validate_target_path,
    wiki_proposal_pending_item_counts,
)


LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
PENDING_WIKI_PROPOSAL_STATUSES = PENDING_REVIEW_STATUSES


class BadRequestError(ValueError):
    pass


class NotFoundError(ValueError):
    pass


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
                self.write_json(ui_memory_detail(self.server.paths, memory_id))
            elif path == "/api/wiki/pages":
                self.write_json(ui_wiki_pages(self.server.paths, query))
            elif path == "/api/wiki/page":
                self.write_json(ui_wiki_page(self.server.paths, query))
            elif path == "/api/wiki/proposal-packets":
                self.write_json(ui_wiki_proposal_packets(self.server.paths, query))
            elif path == "/api/wiki/facts/page":
                self.write_json(ui_wiki_fact_page_review(self.server.paths, query))
            elif path == "/api/wiki/facts":
                self.write_json(ui_wiki_fact_dashboard(self.server.paths))
            elif path == "/api/wiki/proposals":
                self.write_json(ui_wiki_proposals(self.server.paths, query))
            elif path == "/api/cos/policy":
                self.write_json(ui_cos_policy(self.server.paths))
            elif path == "/api/cos/actions":
                self.write_json(ui_cos_actions(self.server.paths, query))
            elif path == "/api/cos/contracts":
                self.write_json(ui_cos_contracts(self.server.paths))
            elif path == "/api/cos/audit":
                self.write_json(ui_cos_audit_status(self.server.paths))
            elif path.startswith("/api/wiki/proposals/"):
                batch_id = path.removeprefix("/api/wiki/proposals/").strip("/")
                self.write_json(ui_wiki_proposal_detail(self.server.paths, batch_id))
            elif path == "/api/review-queue":
                self.write_json(ui_review_queue(self.server.paths))
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
            if parts[:2] == ["wiki", "proposals"]:
                self.dispatch_wiki_proposal_post(parts)
            elif parts[:2] == ["wiki", "proposal-packets"]:
                self.dispatch_wiki_proposal_packet_post(parts)
            elif parts[:2] == ["wiki", "questions"]:
                self.dispatch_wiki_question_post(parts)
            elif parts[:2] == ["wiki", "facts"]:
                self.dispatch_wiki_fact_post(parts)
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

    def dispatch_wiki_proposal_post(self, parts: list[str]) -> None:
        if len(parts) == 2:
            payload = self.read_json_body()
            self.write_json(ui_create_wiki_proposal(self.server.paths, payload))
            return
        if len(parts) < 4:
            self.write_json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)
            return
        batch_id = parts[2]
        action = parts[3:]
        payload = self.read_json_body()
        if action == ["interview"]:
            self.write_json(
                ui_record_wiki_interview(self.server.paths, batch_id, payload)
            )
        elif action == ["interview", "generate"]:
            self.write_json(
                generate_interview_questions(
                    self.server.paths,
                    batch_id,
                    provider_name=optional_str(payload.get("provider")),
                )
            )
        elif action == ["reject"]:
            reason = str(payload.get("reason") or "").strip()
            if not reason:
                raise BadRequestError("reject reason is required")
            self.write_json(
                reject_wiki_proposal(self.server.paths, batch_id, reason=reason)
            )
        elif action == ["apply"]:
            self.write_json(apply_wiki_proposal(self.server.paths, batch_id))
        elif action == ["approve-and-apply"]:
            self.write_json(
                ui_approve_and_apply_wiki_proposal(self.server.paths, batch_id, payload)
            )
        else:
            self.write_json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)

    def dispatch_wiki_proposal_packet_post(self, parts: list[str]) -> None:
        if parts == ["wiki", "proposal-packets", "brief"]:
            payload = self.read_json_body()
            self.write_json(ui_generate_wiki_packet_brief(self.server.paths, payload))
            return
        if parts == ["wiki", "proposal-packets", "facts"]:
            payload = self.read_json_body()
            self.write_json(ui_absorb_wiki_packet_facts(self.server.paths, payload))
            return
        self.write_json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)

    def dispatch_wiki_question_post(self, parts: list[str]) -> None:
        if len(parts) == 4 and parts[3] == "answer":
            payload = self.read_json_body()
            self.write_json(
                ui_answer_wiki_question(self.server.paths, parts[2], payload)
            )
            return
        self.write_json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)

    def dispatch_wiki_fact_post(self, parts: list[str]) -> None:
        if parts == ["wiki", "facts", "reconcile"]:
            payload = self.read_json_body()
            self.write_json(ui_reconcile_wiki_facts(self.server.paths, payload))
            return
        if parts == ["wiki", "facts", "migrate-wiki"]:
            payload = self.read_json_body()
            self.write_json(ui_migrate_wiki_facts(self.server.paths, payload))
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
    paths: BrainPaths, host: str, port: int, token: str | None = None
) -> BrainUIServer:
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
    pending_counts = pending_wiki_proposal_counts(paths)
    if q:
        pages = []
        for result in svc.search_wiki_pages(q, limit=50):
            relative_path = str(result.get("relative_path") or "")
            try:
                target = safe_wiki_path(paths, relative_path)
                page = wiki_page_entry(
                    paths, target, pending_counts.get(relative_path, 0)
                )
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
        pages = wiki_pages_from_index(
            paths, page_type=page_type, status=status, pending_counts=pending_counts
        )
    return {"pages": pages, "count": len(pages)}


def wiki_pages_from_index(
    paths: BrainPaths,
    *,
    page_type: str | None,
    status: str | None,
    pending_counts: dict[str, int],
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
        page = wiki_page_entry(
            paths,
            safe_target,
            pending_counts.get(relative_path, 0),
            indexed_row=dict(row),
        )
        pages.append(page)
    return pages


def wiki_page_entry(
    paths: BrainPaths,
    target: Path,
    pending_count: int,
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
        "pending_proposal_count": pending_count,
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
        "related": list(frontmatter.get("related") or []),
        "pending_proposals": pending_wiki_proposals_for_path(
            paths, target.relative_to(paths.wiki).as_posix()
        ),
    }


def ui_wiki_proposals(paths: BrainPaths, query: dict[str, list[str]]) -> dict[str, Any]:
    service(paths).init_workspace()
    statuses = status_filter_values(first(query, "status"))
    proposals = list_wiki_proposals(
        paths, status=None if len(statuses) != 1 else next(iter(statuses))
    )
    if len(statuses) > 1:
        proposals = [
            proposal for proposal in proposals if proposal.get("status") in statuses
        ]
    pending_counts = wiki_proposal_pending_item_counts(paths)
    proposals = [
        augment_wiki_proposal_summary(
            proposal, pending_counts.get(str(proposal.get("id") or ""), 0)
        )
        for proposal in proposals
    ]
    return {"proposals": proposals, "count": len(proposals)}


def augment_wiki_proposal_summary(
    proposal: dict[str, Any], pending_item_count: int = 0
) -> dict[str, Any]:
    output = dict(proposal)
    output["source_count"] = len(output.get("source_ids") or [])
    output["pending_item_count"] = pending_item_count
    return output


def ui_wiki_proposal_detail(paths: BrainPaths, batch_id: str) -> dict[str, Any]:
    service(paths).init_workspace()
    try:
        proposal = inspect_wiki_proposal(paths, batch_id)
    except ValueError as exc:
        raise NotFoundError(str(exc)) from exc
    output = dict(proposal)
    output["source_documents"] = source_document_summaries(
        paths, output.get("source_ids") or []
    )
    output["source_count"] = len(output.get("source_ids") or [])
    output["items"] = [
        augment_wiki_proposal_item(paths, item) for item in output.get("items", [])
    ]
    return output


def ui_wiki_proposal_packets(
    paths: BrainPaths, query: dict[str, list[str]]
) -> dict[str, Any]:
    service(paths).init_workspace()
    statuses = status_filter_values(first(query, "status")) or set(
        PENDING_WIKI_PROPOSAL_STATUSES
    )
    group_by = first(query, "group_by") or "topic"
    try:
        return list_wiki_review_packets(paths, statuses=statuses, group_by=group_by)
    except ValueError as exc:
        raise BadRequestError(str(exc)) from exc


def ui_generate_wiki_packet_brief(
    paths: BrainPaths, payload: dict[str, Any]
) -> dict[str, Any]:
    service(paths).init_workspace()
    packet_id = str(payload.get("packet_id") or "").strip()
    if not packet_id:
        raise BadRequestError("packet_id is required")
    group_by = str(payload.get("group_by") or "topic").strip()
    answers = object_list_payload(payload.get("answers"))
    try:
        return generate_wiki_review_packet_brief(
            paths,
            packet_id=packet_id,
            group_by=group_by,
            provider_name=optional_str(payload.get("provider")),
            answers=answers,
        )
    except ValueError as exc:
        raise BadRequestError(str(exc)) from exc


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
    return {"counts": counts, "failures": failures}


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


def ui_absorb_wiki_packet_facts(
    paths: BrainPaths, payload: dict[str, Any]
) -> dict[str, Any]:
    service(paths).init_workspace()
    packet_id = str(payload.get("packet_id") or "").strip()
    if not packet_id:
        raise BadRequestError("packet_id is required")
    group_by = str(payload.get("group_by") or "topic").strip()
    overwrite_existing = bool(payload.get("overwrite_existing", False))
    try:
        return absorb_wiki_packet_into_facts(
            paths,
            packet_id=packet_id,
            group_by=group_by,
            overwrite_existing=overwrite_existing,
        )
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


def ui_migrate_wiki_facts(paths: BrainPaths, payload: dict[str, Any]) -> dict[str, Any]:
    service(paths).init_workspace()
    try:
        return migrate_existing_wiki_to_facts(
            paths,
            dry_run=bool(payload.get("dry_run", True)),
            overwrite_existing=bool(payload.get("overwrite_existing", False)),
            include_references=bool(payload.get("include_references", False)),
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


def augment_wiki_proposal_item(
    paths: BrainPaths, item: dict[str, Any]
) -> dict[str, Any]:
    output = dict(item)
    output["source_documents"] = source_document_summaries(
        paths, output.get("source_ids") or []
    )
    output["source_count"] = len(output.get("source_ids") or [])
    try:
        preview = preview_wiki_change_item(paths, output)
    except Exception as exc:
        preview = {
            "target_exists": False,
            "current_markdown": "",
            "current_page_markdown": "",
            "would_be_markdown": "",
            "preview_error": str(exc),
        }
    output.update(preview)
    return output


def preview_wiki_change_item(paths: BrainPaths, item: dict[str, Any]) -> dict[str, Any]:
    target_path = str(item.get("target_path") or "")
    target = safe_wiki_path(paths, target_path, must_exist=False)
    target_exists = target.exists()
    current_page = (
        target.read_text(encoding="utf-8", errors="replace") if target_exists else ""
    )
    operation = str(item.get("operation") or "")
    proposed_markdown = str(item.get("proposed_markdown") or "")
    section_name = str(item.get("section_name") or "")
    preview_error = None
    if operation == "create_page":
        current = ""
        would_be = proposed_markdown.rstrip() + "\n"
        if target_exists:
            preview_error = f"target already exists for create_page: {target_path}"
    elif operation == "replace_page":
        current = current_page
        would_be = proposed_markdown.rstrip() + "\n"
        if not target_exists:
            preview_error = f"target does not exist: {target_path}"
    elif operation == "replace_section":
        current = section_body(current_page, section_name) if target_exists else ""
        would_be = (
            replace_section(current_page, section_name, proposed_markdown)
            if target_exists
            else ""
        )
        if not target_exists:
            preview_error = f"target does not exist: {target_path}"
    elif operation == "append_section":
        current = section_body(current_page, section_name) if target_exists else ""
        would_be = (
            append_to_section(current_page, section_name, proposed_markdown)
            if target_exists
            else ""
        )
        if not target_exists:
            preview_error = f"target does not exist: {target_path}"
    else:
        current = current_page
        would_be = ""
        preview_error = f"unsupported operation: {operation}"
    return {
        "target_exists": target_exists,
        "current_markdown": current,
        "current_page_markdown": current_page,
        "would_be_markdown": would_be,
        "preview_error": preview_error,
    }


def section_body(markdown: str, section_name: str) -> str:
    if not section_name:
        return ""
    match = re.search(
        rf"^##\s+{re.escape(section_name)}\s*\n(.*?)(?=^##\s+|\Z)",
        markdown,
        flags=re.MULTILINE | re.DOTALL,
    )
    return match.group(1).strip() if match else ""


def ui_record_wiki_interview(
    paths: BrainPaths, batch_id: str, payload: dict[str, Any]
) -> dict[str, Any]:
    return record_wiki_interview(
        paths,
        batch_id,
        string_list(payload.get("questions")),
        string_list(payload.get("answers")),
        str(payload.get("disposition") or "needs_interview"),
        provider=optional_str(payload.get("provider")),
        model=optional_str(payload.get("model")),
    )


def ui_approve_and_apply_wiki_proposal(
    paths: BrainPaths, batch_id: str, payload: dict[str, Any]
) -> dict[str, Any]:
    reviewed = record_wiki_interview(
        paths,
        batch_id,
        string_list(payload.get("questions")),
        string_list(payload.get("answers")),
        "approved",
        provider=optional_str(payload.get("provider")),
        model=optional_str(payload.get("model")),
    )
    applied = apply_wiki_proposal(paths, batch_id)
    applied["interview"] = reviewed
    return applied


def ui_create_wiki_proposal(
    paths: BrainPaths, payload: dict[str, Any]
) -> dict[str, Any]:
    title = str(payload.get("title") or "Human wiki edit").strip()
    rationale = str(payload.get("rationale") or "Human-authored browser edit.").strip()
    source_ids = string_list(payload.get("source_ids"))
    changes = payload.get("changes")
    if not isinstance(changes, list) or not changes:
        raise BadRequestError("proposal requires at least one change")
    normalized_changes: list[dict[str, Any]] = []
    for change in changes:
        if not isinstance(change, dict):
            raise BadRequestError("proposal changes must be objects")
        target_path = str(change.get("target_path") or "").strip()
        safe_wiki_path(paths, target_path, must_exist=False)
        validate_target_path(target_path)
        normalized_changes.append(
            {
                "target_path": target_path,
                "operation": str(change.get("operation") or "replace_section"),
                "section_name": optional_str(change.get("section_name")),
                "proposed_markdown": str(change.get("proposed_markdown") or ""),
                "rationale": str(change.get("rationale") or rationale),
                "source_ids": string_list(change.get("source_ids")) or source_ids,
                "confidence": float(change.get("confidence", 1.0)),
            }
        )
    batch_id = service(paths).propose_wiki_update(
        title=title,
        rationale=rationale,
        source_ids=source_ids,
        changes=normalized_changes,
        confidence=float(payload.get("confidence", 1.0)),
        author="human",
        source="ui",
    )
    return {"batch_id": batch_id, "proposal": ui_wiki_proposal_detail(paths, batch_id)}


def ui_review_queue(paths: BrainPaths) -> dict[str, Any]:
    svc = service(paths)
    svc.init_workspace()
    items: list[dict[str, Any]] = []
    for memory in svc.list_memories(status="proposed"):
        source_ids = memory.get("source_ids") or []
        items.append(
            {
                "kind": "memory",
                "id": memory["id"],
                "title": f"{memory['memory_type']} / {memory['scope']}",
                "preview": preview_text(memory.get("content")),
                "status": memory.get("status"),
                "confidence": memory.get("confidence"),
                "created_at": memory.get("created_at"),
                "source_count": len(source_ids),
            }
        )
    review_statuses = {"proposed", "needs_interview"}
    pending_counts = wiki_proposal_pending_item_counts(paths, review_statuses)
    for proposal in list_wiki_proposals(paths):
        if proposal.get("status") not in review_statuses:
            continue
        pending_item_count = pending_counts.get(str(proposal.get("id") or ""), 0)
        if pending_item_count <= 0:
            continue
        items.append(
            {
                "kind": "wiki_proposal",
                "id": proposal["id"],
                "title": proposal.get("title") or proposal["id"],
                "preview": preview_text(proposal.get("rationale")),
                "status": proposal.get("status"),
                "confidence": proposal.get("confidence"),
                "created_at": proposal.get("created_at"),
                "source_count": len(proposal.get("source_ids") or []),
                "pending_item_count": pending_item_count,
            }
        )
    items.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
    return {"items": items, "count": len(items)}


def pending_wiki_proposal_counts(paths: BrainPaths) -> dict[str, int]:
    counts: dict[str, int] = {}
    placeholders = ",".join("?" for _ in PENDING_WIKI_PROPOSAL_STATUSES)
    with connection(paths.sqlite_path) as conn:
        found = list(
            conn.execute(
                f"""
            SELECT i.target_path, COUNT(DISTINCT b.id) AS count
            FROM wiki_change_items i
            JOIN wiki_change_batches b ON b.id = i.batch_id
            WHERE b.status IN ({placeholders}) AND i.status = ?
            GROUP BY i.target_path
            """,
                (*tuple(sorted(PENDING_WIKI_PROPOSAL_STATUSES)), PENDING_ITEM_STATUS),
            )
        )
    for row in found:
        counts[str(row["target_path"])] = int(row["count"])
    return counts


def pending_wiki_proposals_for_path(
    paths: BrainPaths, relative_path: str
) -> list[dict[str, Any]]:
    placeholders = ",".join("?" for _ in PENDING_WIKI_PROPOSAL_STATUSES)
    params = [relative_path, *sorted(PENDING_WIKI_PROPOSAL_STATUSES)]
    with connection(paths.sqlite_path) as conn:
        found = conn.execute(
            f"""
            SELECT DISTINCT b.id, b.title, b.status, b.confidence, b.created_at
            FROM wiki_change_batches b
            JOIN wiki_change_items i ON i.batch_id = b.id
            WHERE i.target_path = ? AND b.status IN ({placeholders}) AND i.status = ?
            ORDER BY b.created_at DESC
            """,
            [*params, PENDING_ITEM_STATUS],
        )
        return [dict(row) for row in found]


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


def object_list_payload(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]


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


def status_filter_values(value: str | None) -> set[str]:
    if not value:
        return set()
    return {part.strip() for part in value.split(",") if part.strip()}


def preview_text(value: Any, limit: int = 180) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "..."


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
    .toolbar input[type="search"] { min-width: 18rem; }
    .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: .75rem; margin-bottom: 1rem; }
    .split { display: grid; grid-template-columns: minmax(280px, 44%) 1fr; gap: .75rem; align-items: start; }
    .stack { display: grid; gap: .75rem; }
    .metric, section {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: .85rem;
    }
    .metric .label { color: var(--muted); font-size: .8rem; }
    .metric .value { font-size: 1.2rem; font-weight: 700; margin-top: .15rem; overflow-wrap: anywhere; }
    .badge {
      display: inline-block;
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: .08rem .45rem;
      font-size: .78rem;
      color: var(--muted);
      background: #f7fafc;
    }
    .row-button {
      border: 0;
      background: transparent;
      padding: 0;
      min-height: 0;
      color: var(--accent);
      text-align: left;
      white-space: normal;
    }
    textarea {
      width: 100%;
      min-height: 12rem;
      padding: .65rem;
      border: 1px solid var(--line);
      border-radius: 6px;
      font: 13px ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      resize: vertical;
    }
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
    .source-list { margin: .5rem 0 0; padding-left: 1.1rem; }
    .source-list li { margin-bottom: .35rem; overflow-wrap: anywhere; }
    .diff { background: #0f1720; color: #dbe6ef; }
    .diff div { white-space: pre-wrap; font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
    .diff .add { color: #b8f7c4; }
    .diff .del { color: #ffb4a8; }
    .diff .ctx { color: #dbe6ef; }
    #message { min-height: 1.25rem; color: var(--muted); }
    @media (max-width: 760px) {
      header { grid-template-columns: 1fr; }
      .auth { grid-template-columns: 1fr 1fr; }
      .auth input { grid-column: 1 / -1; }
      .split { grid-template-columns: 1fr; }
      .toolbar input[type="search"] { min-width: 0; width: 100%; }
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
    <button type="button" data-view="wiki">Wiki</button>
    <button type="button" data-view="packets">Wiki Packets</button>
    <button type="button" data-view="curation">Chief of Staff</button>
    <button type="button" data-view="review">Review</button>
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
      wiki: renderWiki,
      packets: renderPackets,
      curation: renderCuration,
      review: renderReview,
      memory: renderMemory
    };
    let activeView = "status";
    let memoryStatus = "proposed";
    let selectedMemoryId = "";
    const wikiState = {q: "", type: "", status: "", selectedPath: "", edit: false};
    const reviewState = {selectedKind: "", selectedId: "", questions: [], answers: []};
    const packetState = {groupBy: "topic", selectedPacketId: "", selectedTargetPath: "", briefs: {}, loadingPacketId: "", absorbingPacketId: ""};
    const curationState = {selectedQuestionId: "", selectedPagePath: "", lastResult: null, groupBy: "topic", absorbingPacketId: ""};

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

    async function renderWiki() {
      const params = new URLSearchParams();
      if (wikiState.q) params.set("q", wikiState.q);
      if (wikiState.type) params.set("type", wikiState.type);
      if (wikiState.status) params.set("status", wikiState.status);
      const data = await api(`/api/wiki/pages?${params.toString()}`);
      const pages = data.pages || [];
      if (!pages.find(page => page.relative_path === wikiState.selectedPath)) {
        wikiState.selectedPath = pages[0]?.relative_path || "";
        wikiState.edit = false;
      }
      const detail = wikiState.selectedPath ? await wikiPageDetailHtml(wikiState.selectedPath) : `<section><h2>Page</h2><div class="muted">Select a page.</div></section>`;
      app.innerHTML = `
        <section>
          <div class="toolbar">
            <h2>Wiki</h2>
            <input id="wiki-search" type="search" placeholder="Search wiki" value="${escapeHtml(wikiState.q)}">
            <select id="wiki-type">
              <option value="">All types</option>
              ${["index", "project", "concept", "decision", "person", "open_loop", "timeline", "reference"].map(value => `<option value="${value}" ${value === wikiState.type ? "selected" : ""}>${value}</option>`).join("")}
            </select>
            <select id="wiki-status">
              <option value="">All statuses</option>
              ${["draft", "active", "stale", "superseded", "archived"].map(value => `<option value="${value}" ${value === wikiState.status ? "selected" : ""}>${value}</option>`).join("")}
            </select>
            <button type="button" onclick="loadView('wiki')">Refresh</button>
          </div>
          <div class="split">
            <div>
              <table><thead><tr><th>Group</th><th>Page</th><th>Status</th><th>Sources</th></tr></thead>
              <tbody>${pages.map(wikiPageRow).join("") || emptyRow(4)}</tbody></table>
            </div>
            <div>${detail}</div>
          </div>
        </section>`;
      document.getElementById("wiki-search").addEventListener("change", event => {
        wikiState.q = event.target.value.trim();
        wikiState.selectedPath = "";
        loadView("wiki");
      });
      document.getElementById("wiki-type").addEventListener("change", event => {
        wikiState.type = event.target.value;
        wikiState.selectedPath = "";
        loadView("wiki");
      });
      document.getElementById("wiki-status").addEventListener("change", event => {
        wikiState.status = event.target.value;
        wikiState.selectedPath = "";
        loadView("wiki");
      });
      if (wikiState.edit) {
        updateWikiEditor();
      }
    }

    function wikiPageRow(page) {
      const selected = page.relative_path === wikiState.selectedPath ? "ok" : "";
      const pending = page.pending_proposal_count ? ` <span class="badge">${page.pending_proposal_count} pending</span>` : "";
      return `<tr><td>${escapeHtml(wikiGroup(page))}</td><td><button class="row-button ${selected}" type="button" onclick='openWikiPage(${jsString(page.relative_path)})'>${escapeHtml(page.title)}</button><div class="muted">${escapeHtml(page.relative_path)}${pending}</div></td><td>${escapeHtml(page.page_type)}<br><span class="badge">${escapeHtml(page.status)}</span></td><td>${page.source_count}</td></tr>`;
    }

    function wikiGroup(page) {
      if (page.relative_path === "index.md" || page.relative_path === "log.md") return page.relative_path;
      return page.relative_path.split("/")[0] || page.page_type || "";
    }

    async function openWikiPage(path) {
      wikiState.selectedPath = path;
      wikiState.edit = false;
      await loadView("wiki");
    }

    async function wikiPageDetailHtml(path) {
      const page = await api(`/api/wiki/page?path=${encodeURIComponent(path)}`);
      wikiState.currentPage = page;
      const pending = page.pending_proposals || [];
      return `<section>
        <div class="toolbar">
          <h2>${escapeHtml(page.frontmatter.title || page.relative_path)}</h2>
          <span class="badge">${page.generated ? "generated" : "hand-edited"}</span>
          <button type="button" onclick="toggleWikiEdit()">${wikiState.edit ? "Cancel edit" : "Edit via proposal"}</button>
        </div>
        <div class="grid">
          ${metric("Type", page.frontmatter.page_type || "")}
          ${metric("Status", page.frontmatter.status || "")}
          ${metric("Updated", page.frontmatter.updated_at || "")}
          ${metric("Pending Proposals", pending.length)}
        </div>
        ${pending.length ? `<h2>Pending Proposals</h2><table><tbody>${pending.map(proposal => `<tr><td><button class="row-button" type="button" onclick='openReviewItem("wiki_proposal", ${jsString(proposal.id)})'>${escapeHtml(proposal.title)}</button></td><td>${escapeHtml(proposal.status)}</td><td>${proposal.confidence ?? ""}</td></tr>`).join("")}</tbody></table>` : ""}
        <h2>Source Evidence</h2>
        ${sourceDocsList(page.source_documents, page.source_ids)}
        ${wikiState.edit ? wikiEditorHtml(page) : ""}
        <h2>Markdown</h2>
        <pre>${escapeHtml(page.markdown)}</pre>
      </section>`;
    }

    function wikiEditorHtml(page) {
      const headings = extractHeadings(page.body);
      return `<div class="stack">
        <h2>Propose Edit</h2>
        <div class="toolbar">
          <select id="wiki-edit-operation" onchange="updateWikiEditor()">
            <option value="replace_section">Section</option>
            <option value="replace_page">Whole page</option>
          </select>
          <select id="wiki-edit-section" onchange="updateWikiEditor()">
            ${headings.map(heading => `<option value="${escapeHtml(heading)}">${escapeHtml(heading)}</option>`).join("")}
          </select>
          <input id="wiki-edit-title" placeholder="Proposal title" value="${escapeHtml(`Human edit to ${page.frontmatter.title || page.relative_path}`)}">
        </div>
        <input id="wiki-edit-rationale" placeholder="Rationale" value="Human-authored update from browser review.">
        <textarea id="wiki-edit-markdown"></textarea>
        <div class="actions"><button class="primary" type="button" onclick="submitWikiEditProposal()">Create Proposal</button></div>
      </div>`;
    }

    function toggleWikiEdit() {
      wikiState.edit = !wikiState.edit;
      loadView("wiki");
    }

    function updateWikiEditor() {
      const page = wikiState.currentPage;
      const operation = document.getElementById("wiki-edit-operation")?.value || "replace_section";
      const section = document.getElementById("wiki-edit-section");
      const textarea = document.getElementById("wiki-edit-markdown");
      if (!page || !textarea) return;
      if (operation === "replace_page") {
        if (section) section.disabled = true;
        textarea.value = page.markdown;
      } else {
        if (section) section.disabled = false;
        textarea.value = extractSectionBody(page.body, section?.value || "");
      }
    }

    async function submitWikiEditProposal() {
      const page = wikiState.currentPage;
      const operation = document.getElementById("wiki-edit-operation").value;
      const section = document.getElementById("wiki-edit-section").value;
      const proposed = document.getElementById("wiki-edit-markdown").value;
      const title = document.getElementById("wiki-edit-title").value.trim() || `Human edit to ${page.relative_path}`;
      const rationale = document.getElementById("wiki-edit-rationale").value.trim() || "Human-authored browser edit.";
      const change = {
        target_path: page.relative_path,
        operation,
        section_name: operation === "replace_section" ? section : null,
        proposed_markdown: proposed,
        rationale,
        source_ids: page.source_ids,
        confidence: 1.0
      };
      const result = await api("/api/wiki/proposals", {
        method: "POST",
        body: JSON.stringify({title, rationale, source_ids: page.source_ids, changes: [change], confidence: 1.0})
      });
      reviewState.selectedKind = "wiki_proposal";
      reviewState.selectedId = result.batch_id;
      reviewState.questions = [];
      reviewState.answers = [];
      setMessage("Wiki proposal created.");
      await loadView("review");
    }

    async function renderPackets() {
      const params = new URLSearchParams({group_by: packetState.groupBy});
      const data = await api(`/api/wiki/proposal-packets?${params.toString()}`);
      const packets = data.packets || [];
      if (!packets.find(packet => packet.id === packetState.selectedPacketId)) {
        packetState.selectedPacketId = packets[0]?.id || "";
        packetState.selectedTargetPath = "";
      }
      const packet = packets.find(item => item.id === packetState.selectedPacketId);
      if (packet && !packet.pages.find(page => page.target_path === packetState.selectedTargetPath)) {
        packetState.selectedTargetPath = packet.pages[0]?.target_path || "";
      }
      const detail = packet ? packetDetailHtml(packet) : `<section><h2>Review Packet</h2><div class="muted">No pending wiki proposals.</div></section>`;
      app.innerHTML = `
        <section>
          <div class="toolbar">
            <h2>Wiki Packets</h2>
            <select id="packet-group">
              ${["topic", "day", "priority"].map(value => `<option value="${value}" ${value === packetState.groupBy ? "selected" : ""}>Group by ${value}</option>`).join("")}
            </select>
            <button type="button" onclick="loadView('packets')">Refresh</button>
          </div>
          <div class="grid">
            ${metric("Packets", data.totals?.packet_count ?? 0)}
            ${metric("Pages", data.totals?.target_count ?? 0)}
            ${metric("Proposals", data.totals?.batch_count ?? 0)}
            ${metric("Changes", data.totals?.item_count ?? 0)}
            ${metric("Conflict Pages", data.totals?.conflict_target_count ?? 0, data.totals?.conflict_target_count ? "danger" : "")}
            ${metric("Stacked Pages", data.totals?.stacked_target_count ?? 0)}
          </div>
          <div class="split">
            <div><table><thead><tr><th>Packet</th><th>Scope</th><th>Mix</th><th>Latest</th></tr></thead>
            <tbody>${packets.map(packetRow).join("") || emptyRow(4)}</tbody></table></div>
            <div>${detail}</div>
          </div>
        </section>`;
      document.getElementById("packet-group").addEventListener("change", event => {
        packetState.groupBy = event.target.value;
        packetState.selectedPacketId = "";
        packetState.selectedTargetPath = "";
        loadView("packets");
      });
    }

    function packetRow(packet) {
      const selected = packet.id === packetState.selectedPacketId ? "ok" : "";
      return `<tr>
        <td><button class="row-button ${selected}" type="button" onclick='openPacket(${jsString(packet.id)})'>${escapeHtml(packet.label)}</button><div class="muted">${escapeHtml(packet.review_hint || "")}</div></td>
        <td>${packet.target_count} pages<br>${packet.batch_count} proposals<br>${packet.item_count} changes</td>
        <td>${countBadges(packet.operation_counts)}<br>${countBadges(packet.status_counts)}</td>
        <td>${escapeHtml(packet.last_created_at || "")}</td>
      </tr>`;
    }

    async function openPacket(id) {
      packetState.selectedPacketId = id;
      packetState.selectedTargetPath = "";
      await loadView("packets");
    }

    async function openPacketTarget(path) {
      packetState.selectedTargetPath = path;
      await loadView("packets");
    }

    async function openPacketWikiPage(path) {
      wikiState.selectedPath = path;
      wikiState.edit = false;
      await loadView("wiki");
    }

    function packetDetailHtml(packet) {
      const selected = (packet.pages || []).find(page => page.target_path === packetState.selectedTargetPath);
      const brief = packetState.briefs[packet.id];
      const loading = packetState.loadingPacketId === packet.id;
      const absorbing = packetState.absorbingPacketId === packet.id;
      return `<section>
        <div class="toolbar">
          <h2>${escapeHtml(packet.label)}</h2>
          <span class="badge">${packet.simple_page_count} clean</span>
          <span class="badge">${packet.stacked_page_count} stacked</span>
          <span class="badge ${packet.conflict_page_count ? "danger" : ""}">${packet.conflict_page_count} conflict</span>
          <button class="primary" type="button" onclick='generatePacketBrief(${jsString(packet.id)})' ${loading ? "disabled" : ""}>${loading ? "Generating..." : "Generate LLM Brief"}</button>
          <button type="button" onclick='absorbPacketIntoFacts(${jsString(packet.id)})' ${absorbing ? "disabled" : ""}>${absorbing ? "Absorbing..." : "Absorb Into Fact Ledger"}</button>
        </div>
        <p class="muted">${escapeHtml(packet.review_hint || "")}</p>
        ${brief ? packetBriefHtml(packet.id, brief) : ""}
        <table><thead><tr><th>Page</th><th>Shape</th><th>Volume</th><th>Latest</th></tr></thead>
        <tbody>${(packet.pages || []).map(packetPageRow).join("") || emptyRow(4)}</tbody></table>
      </section>
      ${selected ? packetTargetDetailHtml(selected) : ""}`;
    }

    async function generatePacketBrief(packetId, withAnswers = false) {
      const answers = withAnswers ? readPacketBriefAnswers(packetId) : [];
      packetState.loadingPacketId = packetId;
      await loadView("packets");
      setMessage("Generating packet review brief...");
      let finalMessage = "";
      try {
        const brief = await api("/api/wiki/proposal-packets/brief", {
          method: "POST",
          body: JSON.stringify({packet_id: packetId, group_by: packetState.groupBy, provider: null, answers})
        });
        packetState.briefs[packetId] = brief;
        finalMessage = brief.error ? "Generated fallback brief; provider failed." : "Packet brief generated.";
      } catch (error) {
        finalMessage = error.message;
      } finally {
        packetState.loadingPacketId = "";
        await loadView("packets");
        setMessage(finalMessage);
      }
    }

    function readPacketBriefAnswers(packetId) {
      const brief = packetState.briefs[packetId];
      if (!brief) return [];
      return Array.from(document.querySelectorAll(".packet-question-answer")).map((textarea, index) => ({
        target_path: brief.questions?.[index]?.target_path || "",
        question: brief.questions?.[index]?.question || "",
        answer: textarea.value.trim()
      })).filter(item => item.question || item.answer);
    }

    function packetBriefHtml(packetId, brief) {
      return `<section>
        <div class="toolbar">
          <h2>LLM Review Brief</h2>
          <span class="badge">${escapeHtml(brief.provider || "fallback")}</span>
          <span class="badge">${escapeHtml(brief.model || "")}</span>
          ${brief.context?.truncated ? `<span class="badge warn">${brief.context.included_page_count}/${brief.context.page_count} pages included</span>` : ""}
          <button type="button" onclick='generatePacketBrief(${jsString(packetId)}, true)'>Regenerate With Answers</button>
        </div>
        ${brief.error ? `<div class="warn">${escapeHtml(brief.error)}</div>` : ""}
        ${brief.summary?.length ? `<h2>Summary</h2><ul>${brief.summary.map(item => `<li>${escapeHtml(item)}</li>`).join("")}</ul>` : ""}
        ${brief.aggregation_strategy ? `<h2>Aggregation Strategy</h2><p>${escapeHtml(brief.aggregation_strategy)}</p>` : ""}
        ${brief.priority_targets?.length ? `<h2>Priority Targets</h2>${priorityTargetsTable(brief.priority_targets)}` : ""}
        ${brief.conflicts?.length ? `<h2>Conflicts</h2>${briefConflictsTable(brief.conflicts)}` : ""}
        ${brief.questions?.length ? `<h2>Questions</h2>${packetQuestionsHtml(brief.questions)}` : ""}
        ${brief.consolidated_drafts?.length ? `<h2>Consolidated Drafts</h2>${packetDraftsHtml(packetId, brief.consolidated_drafts)}` : ""}
        ${brief.defer_or_reject?.length ? `<h2>Defer / Reject</h2>${jsonBlock(brief.defer_or_reject)}` : ""}
      </section>`;
    }

    function priorityTargetsTable(targets) {
      return `<table><thead><tr><th>Target</th><th>Priority</th><th>Action</th><th>Reason</th></tr></thead>
        <tbody>${targets.map(item => `<tr><td>${escapeHtml(item.target_path || "")}</td><td>${escapeHtml(item.priority || "")}</td><td>${escapeHtml(item.recommended_action || "")}</td><td>${escapeHtml(item.reason || "")}</td></tr>`).join("") || emptyRow(4)}</tbody></table>`;
    }

    function briefConflictsTable(conflicts) {
      return `<table><thead><tr><th>Target</th><th>Issue</th><th>Resolution</th></tr></thead>
        <tbody>${conflicts.map(item => `<tr><td>${escapeHtml(item.target_path || "")}</td><td>${escapeHtml(item.issue || "")}</td><td>${escapeHtml(item.recommended_resolution || item.reason || "")}</td></tr>`).join("") || emptyRow(3)}</tbody></table>`;
    }

    function packetQuestionsHtml(questions) {
      return `<div class="stack">${questions.map((item, index) => `
        <div>
          <div><strong>${escapeHtml(item.target_path || "Packet")}</strong> ${item.blocking ? `<span class="badge warn">blocking</span>` : ""}</div>
          <p>${escapeHtml(item.question || "")}</p>
          ${item.why ? `<div class="muted">${escapeHtml(item.why)}</div>` : ""}
          <textarea class="packet-question-answer" placeholder="Answer ${index + 1}"></textarea>
        </div>`).join("")}</div>`;
    }

    function packetDraftsHtml(packetId, drafts) {
      return `<div class="stack">${drafts.map((draft, index) => `
        <section>
          <div class="toolbar">
            <h2>${escapeHtml(draft.target_path || `Draft ${index + 1}`)}</h2>
            <span class="badge">${escapeHtml(draft.operation || "")}</span>
            ${draft.section_name ? `<span class="badge">${escapeHtml(draft.section_name)}</span>` : ""}
            <span class="badge">${escapeHtml(draft.confidence ?? "")}</span>
            <button type="button" onclick='createPacketDraftProposal(${jsString(packetId)}, ${index})'>Create Proposal From Draft</button>
          </div>
          ${draft.rationale ? `<p>${escapeHtml(draft.rationale)}</p>` : ""}
          ${draft.review_notes ? `<div class="muted">${escapeHtml(draft.review_notes)}</div>` : ""}
          ${draft.source_ids?.length ? `<div class="muted">Sources: ${escapeHtml(draft.source_ids.join(", "))}</div>` : ""}
          ${draft.source_batch_ids?.length ? `<div class="muted">Proposal batches: ${escapeHtml(draft.source_batch_ids.join(", "))}</div>` : ""}
          <textarea readonly>${escapeHtml(draft.proposed_markdown || "")}</textarea>
        </section>`).join("")}</div>`;
    }

    async function createPacketDraftProposal(packetId, draftIndex) {
      const draft = packetState.briefs[packetId]?.consolidated_drafts?.[draftIndex];
      if (!draft) return;
      const title = `Consolidated review draft for ${draft.target_path}`;
      const rationale = draft.rationale || "LLM-assisted consolidated draft from packet review.";
      const change = {
        target_path: draft.target_path,
        operation: draft.operation,
        section_name: draft.section_name || null,
        proposed_markdown: draft.proposed_markdown || "",
        rationale,
        source_ids: draft.source_ids || [],
        confidence: draft.confidence || 0.7
      };
      const result = await api("/api/wiki/proposals", {
        method: "POST",
        body: JSON.stringify({title, rationale, source_ids: draft.source_ids || [], changes: [change], confidence: draft.confidence || 0.7})
      });
      reviewState.selectedKind = "wiki_proposal";
      reviewState.selectedId = result.batch_id;
      setMessage("Draft proposal created.");
      await loadView("review");
    }

    async function absorbPacketIntoFacts(packetId) {
      packetState.absorbingPacketId = packetId;
      await loadView("packets");
      setMessage("Absorbing packet into fact ledger...");
      let finalMessage = "";
      try {
        const result = await api("/api/wiki/proposal-packets/facts", {
          method: "POST",
          body: JSON.stringify({packet_id: packetId, group_by: packetState.groupBy, overwrite_existing: false})
        });
        curationState.lastResult = result;
        curationState.selectedQuestionId = "";
        finalMessage = `Absorbed ${result.candidate_count || 0} candidates into facts.`;
        packetState.absorbingPacketId = "";
        await loadView("curation");
      } catch (error) {
        packetState.absorbingPacketId = "";
        await loadView("packets");
        finalMessage = error.message;
      }
      setMessage(finalMessage);
    }

    function packetPageRow(page) {
      const selected = page.target_path === packetState.selectedTargetPath ? "ok" : "";
      return `<tr>
        <td><button class="row-button ${selected}" type="button" onclick='openPacketTarget(${jsString(page.target_path)})'>${escapeHtml(page.target_path)}</button><div class="muted">${escapeHtml(page.resolution_hint || "")}</div></td>
        <td><span class="badge ${page.complexity === "conflict" ? "danger" : ""}">${escapeHtml(page.complexity)}</span><br>${countBadges(page.operation_counts)}</td>
        <td>${page.batch_count} proposals<br>${page.item_count} changes<br>${page.source_count} sources</td>
        <td>${escapeHtml(page.last_created_at || "")}</td>
      </tr>`;
    }

    function packetTargetDetailHtml(page) {
      return `<section>
        <div class="toolbar">
          <h2>${escapeHtml(page.target_path)}</h2>
          <span class="badge">${escapeHtml(page.topic || "")}</span>
          <span class="badge">${page.target_exists ? "existing page" : "new/missing page"}</span>
          ${page.target_exists ? `<button type="button" onclick='openPacketWikiPage(${jsString(page.target_path)})'>Open Page</button>` : ""}
          ${page.latest_batch_id ? `<button type="button" onclick='openReviewItem("wiki_proposal", ${jsString(page.latest_batch_id)})'>Open Latest Proposal</button>` : ""}
        </div>
        <div class="grid">
          ${metric("Complexity", page.complexity)}
          ${metric("Proposals", page.batch_count)}
          ${metric("Changes", page.item_count)}
          ${metric("Sources", page.source_count)}
        </div>
        ${page.conflicts?.length ? `<div class="warn">${escapeHtml(page.conflicts.join("; "))}</div>` : ""}
        <p>${escapeHtml(page.resolution_hint || "")}</p>
        <h2>Operation Groups</h2>
        <table><thead><tr><th>Operation</th><th>Items</th><th>Latest</th><th>Resolution</th></tr></thead>
        <tbody>${(page.operation_groups || []).map(operationGroupRow).join("") || emptyRow(4)}</tbody></table>
        <h2>Proposal History</h2>
        <table><thead><tr><th>Proposal</th><th>Status</th><th>Shape</th><th>Action</th></tr></thead>
        <tbody>${(page.proposals || []).map(packetProposalRow).join("") || emptyRow(4)}</tbody></table>
      </section>`;
    }

    function operationGroupRow(group) {
      const section = group.section_name ? `<div class="muted">${escapeHtml(group.section_name)}</div>` : "";
      return `<tr>
        <td>${escapeHtml(group.operation)}${section}</td>
        <td>${group.item_count} items<br>${group.old_revision_count || 0} older revisions<br>${group.duplicate_append_count || 0} duplicate appends</td>
        <td>${escapeHtml(group.latest_created_at || "")}<div class="muted">${escapeHtml(group.latest_title || "")}</div></td>
        <td>${escapeHtml(group.resolution || "")}</td>
      </tr>`;
    }

    function packetProposalRow(proposal) {
      return `<tr>
        <td><button class="row-button" type="button" onclick='openReviewItem("wiki_proposal", ${jsString(proposal.id)})'>${escapeHtml(proposal.title)}</button><div class="muted">${escapeHtml(proposal.preview || "")}</div></td>
        <td>${escapeHtml(proposal.status || "")}<br><span class="badge">${proposal.confidence ?? ""}</span><br>${escapeHtml(proposal.created_at || "")}</td>
        <td>${proposal.item_count} changes<br>${countBadges(proposal.operation_counts)}</td>
        <td><button type="button" onclick='openReviewItem("wiki_proposal", ${jsString(proposal.id)})'>Open</button></td>
      </tr>`;
    }

    async function renderCuration() {
      const [data, backlog] = await Promise.all([
        api("/api/wiki/facts"),
        api(`/api/wiki/proposal-packets?${new URLSearchParams({group_by: curationState.groupBy}).toString()}`)
      ]);
      const questions = data.open_questions || [];
      const pages = data.managed_pages || [];
      if (!questions.find(question => question.id === curationState.selectedQuestionId)) {
        curationState.selectedQuestionId = questions[0]?.id || "";
      }
      if (!pages.find(page => page.relative_path === curationState.selectedPagePath)) {
        curationState.selectedPagePath = pages[0]?.relative_path || "";
      }
      const selected = questions.find(question => question.id === curationState.selectedQuestionId);
      const selectedPage = curationState.selectedPagePath
        ? await api(`/api/wiki/facts/page?path=${encodeURIComponent(curationState.selectedPagePath)}`)
        : null;
      const packets = backlog.packets || [];
      app.innerHTML = `
        <section>
          <div class="toolbar">
            <h2>Chief of Staff</h2>
            <button type="button" onclick="loadView('curation')">Refresh</button>
            <button class="primary" type="button" onclick="regenerateCurationPage(false)" ${selectedPage ? "" : "disabled"}>Regenerate Page</button>
            <button type="button" onclick="regenerateCurationPage(true)" ${selectedPage ? "" : "disabled"}>Preview Page</button>
            <button type="button" onclick="migrateExistingWikiFacts(true)">Preview Wiki Migration</button>
            <button type="button" onclick="migrateExistingWikiFacts(false)">Backfill Wiki Facts</button>
            <button type="button" onclick="reconcileChiefOfStaffQuestions()">Reconcile Duplicates</button>
          </div>
          <div class="grid">
            ${metric("Facts", data.counts?.facts ?? 0)}
            ${metric("Active", data.counts?.by_status?.active ?? 0, "ok")}
            ${metric("Managed Pages", pages.length)}
            ${metric("Conflicted", data.counts?.by_status?.conflicted ?? 0, data.counts?.by_status?.conflicted ? "danger" : "")}
            ${metric("Open Questions", data.counts?.questions_by_status?.open ?? 0, data.counts?.questions_by_status?.open ? "warn" : "ok")}
            ${metric("Backlog Changes", backlog.totals?.item_count ?? 0, backlog.totals?.item_count ? "warn" : "ok")}
          </div>
          ${curationState.lastResult ? lastCurationResultHtml(curationState.lastResult) : ""}
          <div class="split">
            <div>
              <h2>Open Questions</h2>
              <table><thead><tr><th>Question</th><th>Facts</th><th>Created</th></tr></thead>
              <tbody>${questions.map(curationQuestionRow).join("") || emptyRow(3)}</tbody></table>
            </div>
            <div>${selected ? curationQuestionDetailHtml(selected) : `<section><h2>Question</h2><div class="muted">No open factual conflicts.</div></section>`}</div>
          </div>
          <h2>Managed Pages</h2>
          <div class="split">
            <div>
              <table><thead><tr><th>Page</th><th>Facts</th><th>Status</th></tr></thead>
              <tbody>${pages.map(curationPageRow).join("") || emptyRow(3)}</tbody></table>
            </div>
            <div>${selectedPage ? curationPageDetailHtml(selectedPage) : `<section><h2>Managed Page</h2><div class="muted">No managed pages yet.</div></section>`}</div>
          </div>
          <h2>Proposal Backlog Drain</h2>
          ${curationBacklogHtml(backlog, packets)}
          <h2>Recent Facts</h2>
          ${recentFactsTable(data.recent_facts || [])}
          <h2>Recent Curation Runs</h2>
          ${curationRunsTable(data.recent_runs || [])}
        </section>`;
      document.getElementById("curation-packet-group")?.addEventListener("change", event => {
        curationState.groupBy = event.target.value;
        curationState.absorbingPacketId = "";
        loadView("curation");
      });
    }

    function lastCurationResultHtml(result) {
      const pages = result.curation?.pages || [];
      return `<section>
        <div class="toolbar">
          <h2>Last Curation Output</h2>
          <span class="badge">${escapeHtml(result.packet?.label || "")}</span>
          ${result.migration ? `<span class="badge">${escapeHtml(result.migration)}</span>` : ""}
          ${result.dry_run ? `<span class="badge">dry run</span>` : ""}
          <span class="badge">${result.new_candidate_count ?? result.candidate_count ?? 0} candidates</span>
          <span class="badge">${result.skipped_existing || 0} skipped</span>
          <span class="badge">${result.page_count || 0} pages</span>
          <span class="badge">${result.created_fact_ids?.length || 0} new facts</span>
          <span class="badge">${result.auto_merged || 0} merged</span>
          <span class="badge">${result.resolved?.auto_superseded || 0} auto-superseded</span>
          <span class="badge">${result.resolved?.created_question_ids?.length || 0} questions</span>
        </div>
        ${result.curation?.lint_errors?.length ? `<div class="warn">${escapeHtml(result.curation.lint_errors.join("; "))}</div>` : ""}
        <div class="stack">${pages.map(curatedPagePreviewHtml).join("") || `<div class="muted">No pages generated.</div>`}</div>
      </section>`;
    }

    function curatedPagePreviewHtml(page) {
      return `<section>
        <div class="toolbar">
          <h2>${escapeHtml(page.relative_path || page.page_hint || "Draft")}</h2>
          <span class="badge ${page.written ? "ok" : "warn"}">${page.written ? "written" : "preview"}</span>
          ${page.reason ? `<span class="badge">${escapeHtml(page.reason)}</span>` : ""}
        </div>
        <textarea readonly>${escapeHtml(page.markdown || "")}</textarea>
      </section>`;
    }

    function curationQuestionRow(question) {
      const selected = question.id === curationState.selectedQuestionId ? "ok" : "";
      return `<tr>
        <td><button class="row-button ${selected}" type="button" onclick='openCurationQuestion(${jsString(question.id)})'>${escapeHtml(question.question)}</button><div class="muted">${escapeHtml(question.page_hint || question.entity_key || "")}</div></td>
        <td>${(question.fact_ids || []).length}</td>
        <td>${escapeHtml(question.created_at || "")}</td>
      </tr>`;
    }

    async function openCurationQuestion(id) {
      curationState.selectedQuestionId = id;
      await loadView("curation");
    }

    async function openCurationQuestionPage(path) {
      curationState.selectedPagePath = path;
      await loadView("curation");
    }

    function curationQuestionDetailHtml(question) {
      return `<section>
        <div class="toolbar">
          <h2>${escapeHtml(question.page_hint || "Question")}</h2>
          <span class="badge">${escapeHtml(question.kind || "")}</span>
          ${question.page_hint ? `<button type="button" onclick='openCurationQuestionPage(${jsString(question.page_hint)})'>Review Page</button>` : ""}
        </div>
        <p>${escapeHtml(question.question || "")}</p>
        <div class="stack">${(question.options || []).map(option => `
          <section>
            <div class="toolbar">
              <span class="badge">${escapeHtml(option.observed_at || "undated")}</span>
              <span class="badge">${escapeHtml(option.confidence ?? "")}</span>
              <button class="primary" type="button" onclick='answerWikiQuestion(${jsString(question.id)}, ${jsString(option.fact_id)}, "")'>Use This Fact</button>
            </div>
            <p>${escapeHtml(option.statement || "")}</p>
            ${option.source_ids?.length ? `<div class="muted">Sources: ${escapeHtml(option.source_ids.join(", "))}</div>` : ""}
          </section>`).join("")}</div>
        <h2>Different Answer</h2>
        <textarea id="curation-answer" placeholder="State the fact that should be treated as current."></textarea>
        <div class="actions"><button type="button" onclick='answerWikiQuestion(${jsString(question.id)}, "", document.getElementById("curation-answer").value.trim())'>Save Answer</button></div>
      </section>`;
    }

    async function answerWikiQuestion(questionId, selectedFactId, answer) {
      const body = selectedFactId ? {selected_fact_id: selectedFactId, answer} : {answer};
      await api(`/api/wiki/questions/${encodeURIComponent(questionId)}/answer`, {
        method: "POST",
        body: JSON.stringify(body)
      });
      curationState.selectedQuestionId = "";
      curationState.lastResult = null;
      await loadView("curation");
      setMessage("Question answered and affected managed page refreshed.");
    }

    async function reconcileChiefOfStaffQuestions() {
      const result = await api("/api/wiki/facts/reconcile", {
        method: "POST",
        body: JSON.stringify({overwrite_existing: false})
      });
      curationState.selectedQuestionId = "";
      curationState.lastResult = null;
      await loadView("curation");
      setMessage(`Merged ${result.merged_facts || 0} duplicate facts; dismissed ${result.dismissed_question_ids?.length || 0} questions.`);
    }

    async function migrateExistingWikiFacts(dryRun) {
      const result = await api("/api/wiki/facts/migrate-wiki", {
        method: "POST",
        body: JSON.stringify({dry_run: dryRun, overwrite_existing: false})
      });
      curationState.selectedQuestionId = "";
      curationState.lastResult = result;
      await loadView("curation");
      const verb = dryRun ? "Previewed" : "Backfilled";
      setMessage(`${verb} ${result.new_candidate_count || 0} new wiki facts from ${result.page_count || 0} pages.`);
    }

    function curationPageRow(page) {
      const selected = page.relative_path === curationState.selectedPagePath ? "ok" : "";
      return `<tr>
        <td><button class="row-button ${selected}" type="button" onclick='openCurationPage(${jsString(page.relative_path)})'>${escapeHtml(page.title || page.relative_path)}</button><div class="muted">${escapeHtml(page.relative_path)}</div></td>
        <td>${page.active_fact_count || 0} active<br>${page.open_question_count || 0} open questions</td>
        <td><span class="badge ${page.managed ? "ok" : "warn"}">${page.managed ? "managed" : "draft only"}</span><br>${escapeHtml(page.status || "")}</td>
      </tr>`;
    }

    async function openCurationPage(path) {
      curationState.selectedPagePath = path;
      await loadView("curation");
    }

    function curationPageDetailHtml(page) {
      return `<section>
        <div class="toolbar">
          <h2>${escapeHtml(page.frontmatter?.title || page.relative_path)}</h2>
          <span class="badge ${page.managed ? "ok" : "warn"}">${page.managed ? "managed" : "not managed"}</span>
          <span class="badge">${page.would_change ? "changes pending" : "current"}</span>
          <button type="button" onclick='openPacketWikiPage(${jsString(page.relative_path)})'>Open In Wiki</button>
        </div>
        <div class="grid">
          ${metric("Active Facts", page.active_facts?.length || 0)}
          ${metric("All Facts", page.facts?.length || 0)}
          ${metric("Snapshots", page.snapshots?.length || 0)}
          ${metric("Can Write", page.can_write ? "yes" : "no", page.can_write ? "ok" : "warn")}
        </div>
        ${!page.can_write ? `<div class="warn">This page is not managed yet. Regeneration will preview only unless overwrite is explicitly enabled.</div>` : ""}
        <h2>Current vs Draft</h2>
        ${serverDiffHtml(page.diff)}
        <details>
          <summary>Draft Markdown</summary>
          <textarea readonly>${escapeHtml(page.draft_markdown || "")}</textarea>
        </details>
        <h2>Confirmed Correction</h2>
        ${curationCorrectionHtml(page)}
        <h2>Page Facts</h2>
        ${curationFactsTable(page.facts || [])}
        <h2>Snapshots</h2>
        ${curationSnapshotsTable(page.snapshots || [])}
      </section>`;
    }

    function curationCorrectionHtml(page) {
      const activeFacts = (page.facts || []).filter(fact => fact.status === "active");
      return `<div class="stack">
        <textarea id="curation-correction" placeholder="State a confirmed fact or correction for this page."></textarea>
        <div class="toolbar">
          <select id="curation-correction-section">
            ${["Summary", "Key Points", "Current State", "Decision", "Definition", "Open Loops", "Source Evidence"].map(section => `<option value="${section}">${section}</option>`).join("")}
          </select>
          <label><input id="curation-correction-overwrite" type="checkbox"> overwrite unmanaged page</label>
        </div>
        ${activeFacts.length ? `<div class="stack">${activeFacts.map(fact => `
          <label><input class="supersede-fact" type="checkbox" value="${escapeHtml(fact.id)}"> supersede ${escapeHtml(fact.statement || "")}</label>
        `).join("")}</div>` : `<div class="muted">No active facts to supersede.</div>`}
        <div class="actions"><button class="primary" type="button" onclick="submitCurationCorrection()">Add Confirmed Fact</button></div>
      </div>`;
    }

    function curationFactsTable(facts) {
      return `<table><thead><tr><th>Status</th><th>Section</th><th>Fact</th><th>Evidence</th></tr></thead>
        <tbody>${facts.map(fact => `<tr>
          <td><span class="badge ${fact.status === "active" ? "ok" : fact.status === "conflicted" ? "danger" : ""}">${escapeHtml(fact.status || "")}</span><br>${fact.confirmed_by_user ? `<span class="badge ok">confirmed</span>` : ""}</td>
          <td>${escapeHtml(fact.section_hint || "")}</td>
          <td>${escapeHtml(fact.statement || "")}<div class="muted">${escapeHtml(fact.id || "")}</div></td>
          <td>${(fact.source_ids || []).length} sources<br><span class="badge">${escapeHtml(fact.confidence ?? "")}</span></td>
        </tr>`).join("") || emptyRow(4)}</tbody></table>`;
    }

    function curationSnapshotsTable(snapshots) {
      return `<table><thead><tr><th>Snapshot</th><th>Reason</th><th>Preview</th><th>Action</th></tr></thead>
        <tbody>${snapshots.map(snapshot => `<tr>
          <td>${escapeHtml(snapshot.created_at || "")}<div class="muted">${escapeHtml(snapshot.id || "")}</div></td>
          <td>${escapeHtml(snapshot.reason || "")}</td>
          <td><div class="muted">${escapeHtml(snapshot.before_preview || "")}</div><div>${escapeHtml(snapshot.after_preview || "")}</div></td>
          <td><button type="button" onclick='revertCurationSnapshot(${jsString(snapshot.id)})'>Revert</button></td>
        </tr>`).join("") || emptyRow(4)}</tbody></table>`;
    }

    function serverDiffHtml(diff) {
      const lines = diff?.lines || [];
      if (!lines.length) return `<pre class="diff"><div class="ctx">No changes.</div></pre>`;
      return `<pre class="diff">${lines.map(line => {
        const klass = line.startsWith("+") && !line.startsWith("+++") ? "add" : line.startsWith("-") && !line.startsWith("---") ? "del" : "ctx";
        return `<div class="${klass}">${escapeHtml(line)}</div>`;
      }).join("")}${diff.truncated ? `<div class="warn">Diff truncated after ${diff.lines.length} lines.</div>` : ""}</pre>`;
    }

    async function regenerateCurationPage(dryRun) {
      if (!curationState.selectedPagePath) return;
      const result = await api("/api/wiki/facts/pages/regenerate", {
        method: "POST",
        body: JSON.stringify({page_hint: curationState.selectedPagePath, dry_run: dryRun, overwrite_existing: false})
      });
      curationState.lastResult = result;
      await loadView("curation");
      setMessage(dryRun ? "Managed page preview refreshed." : "Managed page regenerated.");
    }

    async function revertCurationSnapshot(snapshotId) {
      const result = await api("/api/wiki/facts/pages/revert", {
        method: "POST",
        body: JSON.stringify({snapshot_id: snapshotId})
      });
      curationState.selectedPagePath = result.review?.relative_path || curationState.selectedPagePath;
      curationState.lastResult = result;
      await loadView("curation");
      setMessage("Managed page reverted to snapshot.");
    }

    async function submitCurationCorrection() {
      const statement = document.getElementById("curation-correction").value.trim();
      const section = document.getElementById("curation-correction-section").value;
      const overwrite = document.getElementById("curation-correction-overwrite").checked;
      const supersede = Array.from(document.querySelectorAll(".supersede-fact:checked")).map(input => input.value);
      const result = await api("/api/wiki/facts/corrections", {
        method: "POST",
        body: JSON.stringify({
          page_hint: curationState.selectedPagePath,
          statement,
          section_hint: section,
          supersede_fact_ids: supersede,
          overwrite_existing: overwrite
        })
      });
      curationState.lastResult = result;
      await loadView("curation");
      setMessage("Confirmed fact added and managed page refreshed.");
    }

    function curationBacklogHtml(backlog, packets) {
      return `<section>
        <div class="toolbar">
          <select id="curation-packet-group">
            ${["topic", "day", "priority"].map(value => `<option value="${value}" ${value === curationState.groupBy ? "selected" : ""}>Group by ${value}</option>`).join("")}
          </select>
          <button type="button" onclick="loadView('curation')">Refresh Backlog</button>
        </div>
        <table><thead><tr><th>Packet</th><th>Scope</th><th>Mix</th><th>Action</th></tr></thead>
        <tbody>${packets.map(curationBacklogRow).join("") || emptyRow(4)}</tbody></table>
      </section>`;
    }

    function curationBacklogRow(packet) {
      const absorbing = curationState.absorbingPacketId === packet.id;
      return `<tr>
        <td>${escapeHtml(packet.label || packet.id)}<div class="muted">${escapeHtml(packet.review_hint || "")}</div></td>
        <td>${packet.target_count || 0} pages<br>${packet.batch_count || 0} proposals<br>${packet.item_count || 0} changes</td>
        <td>${countBadges(packet.operation_counts || {})}<br>${countBadges(packet.status_counts || {})}</td>
        <td><button type="button" onclick='absorbCurationPacket(${jsString(packet.id)})' ${absorbing ? "disabled" : ""}>${absorbing ? "Absorbing..." : "Absorb"}</button></td>
      </tr>`;
    }

    async function absorbCurationPacket(packetId) {
      curationState.absorbingPacketId = packetId;
      await loadView("curation");
      const result = await api("/api/wiki/proposal-packets/facts", {
        method: "POST",
        body: JSON.stringify({packet_id: packetId, group_by: curationState.groupBy, overwrite_existing: false})
      });
      curationState.absorbingPacketId = "";
      curationState.lastResult = result;
      await loadView("curation");
      setMessage(`Absorbed ${result.candidate_count || 0} candidates into facts.`);
    }

    function recentFactsTable(facts) {
      return `<table><thead><tr><th>Status</th><th>Page</th><th>Fact</th><th>Evidence</th></tr></thead>
        <tbody>${facts.map(fact => `<tr>
          <td><span class="badge ${fact.status === "conflicted" ? "danger" : fact.status === "active" ? "ok" : ""}">${escapeHtml(fact.status || "")}</span><br>${escapeHtml(fact.observed_at || "")}</td>
          <td>${escapeHtml(fact.page_hint || "")}<div class="muted">${escapeHtml(fact.section_hint || "")}</div></td>
          <td>${escapeHtml(fact.statement || "")}</td>
          <td>${(fact.source_ids || []).length} sources<br><span class="badge">${escapeHtml(fact.confidence ?? "")}</span></td>
        </tr>`).join("") || emptyRow(4)}</tbody></table>`;
    }

    function curationRunsTable(runs) {
      return `<table><thead><tr><th>Run</th><th>Packet</th><th>Outcome</th><th>Created</th></tr></thead>
        <tbody>${runs.map(run => `<tr>
          <td>${escapeHtml(run.id)}</td>
          <td>${escapeHtml(run.source_packet_id || "")}<div class="muted">${escapeHtml(run.group_by || "")}</div></td>
          <td>${escapeHtml(run.status || "")}<br>${countBadges(run.summary || {})}</td>
          <td>${escapeHtml(run.created_at || "")}</td>
        </tr>`).join("") || emptyRow(4)}</tbody></table>`;
    }

    async function renderReview() {
      const data = await api("/api/review-queue");
      const items = data.items || [];
      if (!reviewState.selectedId && items[0]) {
        reviewState.selectedKind = items[0].kind;
        reviewState.selectedId = items[0].id;
      }
      const selected = items.find(item => item.kind === reviewState.selectedKind && item.id === reviewState.selectedId);
      const detail = selected ? await reviewDetailHtml(selected.kind, selected.id) : `<section><h2>Review Detail</h2><div class="muted">Select an item.</div></section>`;
      app.innerHTML = `
        <section>
          <div class="toolbar"><h2>Review</h2><button type="button" onclick="loadView('review')">Refresh</button></div>
          <div class="split">
            <div><table><thead><tr><th>Kind</th><th>Item</th><th>Status</th><th>Actions</th></tr></thead>
            <tbody>${items.map(reviewRow).join("") || emptyRow(4)}</tbody></table></div>
            <div>${detail}</div>
          </div>
        </section>`;
    }

    function reviewRow(item) {
      const actions = item.kind === "memory"
        ? `<div class="actions"><button type="button" onclick='openReviewItem("memory", ${jsString(item.id)})'>Open</button><button type="button" onclick='reviewMemoryAction(${jsString(item.id)}, "approve")'>Approve</button><button type="button" onclick='reviewRejectMemory(${jsString(item.id)})'>Reject</button><button type="button" onclick='reviewMemoryAction(${jsString(item.id)}, "archive")'>Archive</button></div>`
        : `<div class="actions"><button type="button" onclick='openReviewItem("wiki_proposal", ${jsString(item.id)})'>Open</button><button type="button" onclick='rejectWikiProposal(${jsString(item.id)})'>Reject</button></div>`;
      return `<tr><td>${escapeHtml(item.kind)}</td><td><button class="row-button" type="button" onclick='openReviewItem(${jsString(item.kind)}, ${jsString(item.id)})'>${escapeHtml(item.title)}</button><div class="muted">${escapeHtml(item.preview)}</div></td><td>${escapeHtml(item.status)}<br><span class="badge">${item.confidence ?? ""}</span><br><span class="muted">${item.source_count} sources</span></td><td>${actions}</td></tr>`;
    }

    async function openReviewItem(kind, id) {
      reviewState.selectedKind = kind;
      reviewState.selectedId = id;
      reviewState.questions = [];
      reviewState.answers = [];
      await loadView("review");
    }

    async function reviewDetailHtml(kind, id) {
      if (kind === "memory") return memoryDetailHtml(id);
      return wikiProposalDetailHtml(id);
    }

    async function wikiProposalDetailHtml(id) {
      const proposal = await api(`/api/wiki/proposals/${encodeURIComponent(id)}`);
      const questions = reviewState.questions.length ? reviewState.questions : ["", "", ""];
      const answers = reviewState.answers.length ? reviewState.answers : questions.map(() => "");
      return `<section>
        <div class="toolbar">
          <h2>${escapeHtml(proposal.title)}</h2>
          <span class="badge">${escapeHtml(proposal.status)}</span>
          <button type="button" onclick='generateWikiQuestions(${jsString(id)})'>Generate questions</button>
          <button type="button" onclick='rejectWikiProposal(${jsString(id)})'>Reject</button>
        </div>
        <div class="grid">
          ${metric("Author", proposal.author || "")}
          ${metric("Source", proposal.source || "")}
          ${metric("Confidence", proposal.confidence ?? "")}
          ${metric("Created", proposal.created_at || "")}
        </div>
        <p>${escapeHtml(proposal.rationale)}</p>
        <h2>Source Evidence</h2>
        ${sourceDocsList(proposal.source_documents, proposal.source_ids)}
        <h2>Changes</h2>
        ${(proposal.items || []).map(wikiProposalItemHtml).join("")}
        <h2>Interview</h2>
        ${interviewFormHtml(questions, answers)}
        <div class="actions">
          <button type="button" onclick='recordWikiInterview(${jsString(id)}, "needs_interview")'>Save Interview</button>
          <button class="primary" type="button" onclick='approveAndApplyWikiProposal(${jsString(id)})'>Approve &amp; Apply</button>
        </div>
        ${(proposal.interviews || []).length ? `<h2>Existing Interviews</h2>${jsonBlock(proposal.interviews)}` : ""}
      </section>`;
    }

    function wikiProposalItemHtml(item, index) {
      const before = item.current_page_markdown || item.current_markdown || "";
      const after = item.would_be_markdown || "";
      return `<section>
        <div class="toolbar"><h2>Change ${index + 1}</h2><span class="badge">${escapeHtml(item.operation)}</span><span class="badge">${escapeHtml(item.target_path)}</span></div>
        ${item.section_name ? `<div class="muted">Section: ${escapeHtml(item.section_name)}</div>` : ""}
        ${item.preview_error ? `<div class="warn">${escapeHtml(item.preview_error)}</div>` : ""}
        <p>${escapeHtml(item.rationale || "")}</p>
        ${sourceDocsList(item.source_documents, item.source_ids)}
        ${lineDiff(before, after)}
      </section>`;
    }

    function interviewFormHtml(questions, answers) {
      return `<div class="stack">${questions.map((question, index) => `
        <div>
          <input class="question-input" value="${escapeHtml(question)}" placeholder="Question ${index + 1}">
          <textarea class="answer-input" placeholder="Answer ${index + 1}">${escapeHtml(answers[index] || "")}</textarea>
        </div>`).join("")}</div>`;
    }

    function readInterviewForm() {
      const questions = Array.from(document.querySelectorAll(".question-input")).map(input => input.value.trim()).filter(Boolean);
      const answers = Array.from(document.querySelectorAll(".answer-input")).map(input => input.value.trim());
      return {questions, answers};
    }

    async function generateWikiQuestions(id) {
      const data = await api(`/api/wiki/proposals/${encodeURIComponent(id)}/interview/generate`, {method: "POST", body: JSON.stringify({provider: null})});
      reviewState.questions = data.questions || [];
      reviewState.answers = reviewState.questions.map(() => "");
      await loadView("review");
    }

    async function recordWikiInterview(id, disposition) {
      const form = readInterviewForm();
      await api(`/api/wiki/proposals/${encodeURIComponent(id)}/interview`, {method: "POST", body: JSON.stringify({...form, disposition})});
      await loadView("review");
      setMessage("Interview saved.");
    }

    async function approveAndApplyWikiProposal(id) {
      const form = readInterviewForm();
      const result = await api(`/api/wiki/proposals/${encodeURIComponent(id)}/approve-and-apply`, {method: "POST", body: JSON.stringify(form)});
      if (result.lint?.errors?.length) {
        setMessage("Apply failed lint.");
        return;
      }
      reviewState.selectedId = "";
      reviewState.questions = [];
      reviewState.answers = [];
      await loadView("review");
      setMessage("Proposal approved and applied.");
    }

    async function rejectWikiProposal(id) {
      const reason = prompt("Reject reason");
      if (!reason) return;
      await api(`/api/wiki/proposals/${encodeURIComponent(id)}/reject`, {method: "POST", body: JSON.stringify({reason})});
      reviewState.selectedId = "";
      reviewState.questions = [];
      reviewState.answers = [];
      await loadView(activeView);
      setMessage("Wiki proposal rejected.");
    }

    async function renderMemory() {
      const data = await api(`/api/memory?status=${encodeURIComponent(memoryStatus)}`);
      const memories = data.memories || [];
      if (!selectedMemoryId && memories[0]) selectedMemoryId = memories[0].id;
      const detail = selectedMemoryId ? await memoryDetailHtml(selectedMemoryId) : "";
      app.innerHTML = `
        <section>
          <div class="toolbar">
            <h2>Memory Review</h2>
            <select id="memory-status">
              ${["proposed", "active", "rejected", "archived"].map(value => `<option value="${value}" ${value === memoryStatus ? "selected" : ""}>${value}</option>`).join("")}
            </select>
            <button type="button" onclick="loadView('memory')">Refresh</button>
          </div>
          <div class="split">
            <table><thead><tr><th>ID</th><th>Type</th><th>Scope</th><th>Content</th><th>Actions</th></tr></thead>
            <tbody>${memories.map(memoryRow).join("") || emptyRow(5)}</tbody></table>
            <div>${detail}</div>
          </div>
        </section>`;
      document.getElementById("memory-status").addEventListener("change", event => {
        memoryStatus = event.target.value;
        selectedMemoryId = "";
        loadView("memory");
      });
    }

    function memoryRow(memory) {
      const actions = memory.status === "proposed"
        ? `<div class="actions"><button type="button" onclick='openMemoryDetail(${jsString(memory.id)})'>Open</button><button type="button" onclick='memoryAction(${jsString(memory.id)}, "approve")'>Approve</button><button type="button" onclick='rejectMemory(${jsString(memory.id)})'>Reject</button><button type="button" onclick='memoryAction(${jsString(memory.id)}, "archive")'>Archive</button></div>`
        : `<div class="actions"><button type="button" onclick='openMemoryDetail(${jsString(memory.id)})'>Open</button><button type="button" onclick='memoryAction(${jsString(memory.id)}, "archive")'>Archive</button></div>`;
      return `<tr><td>${escapeHtml(memory.id)}</td><td>${escapeHtml(memory.memory_type)}</td><td>${escapeHtml(memory.scope)}</td><td>${escapeHtml(memory.content)}</td><td>${actions}</td></tr>`;
    }

    async function openMemoryDetail(id) {
      selectedMemoryId = id;
      await loadView("memory");
    }

    async function memoryDetailHtml(id) {
      const memory = await api(`/api/memory/${encodeURIComponent(id)}`);
      const actions = memory.status === "proposed"
        ? `<div class="actions"><button type="button" onclick='memoryAction(${jsString(id)}, "approve")'>Approve</button><button type="button" onclick='rejectMemory(${jsString(id)})'>Reject</button><button type="button" onclick='memoryAction(${jsString(id)}, "archive")'>Archive</button></div>`
        : `<div class="actions"><button type="button" onclick='memoryAction(${jsString(id)}, "archive")'>Archive</button></div>`;
      return `<section>
        <div class="toolbar"><h2>${escapeHtml(memory.id)}</h2><span class="badge">${escapeHtml(memory.status)}</span>${actions}</div>
        <div class="grid">
          ${metric("Type", memory.memory_type)}
          ${metric("Scope", memory.scope)}
          ${metric("Confidence", memory.confidence)}
          ${metric("Reviewed", memory.reviewed_at || "")}
        </div>
        <pre>${escapeHtml(memory.content)}</pre>
        ${memory.review_reason ? `<p class="muted">${escapeHtml(memory.review_reason)}</p>` : ""}
        <h2>Source Evidence</h2>
        ${sourceDocsList(memory.source_documents, memory.source_ids)}
        ${(memory.audit?.errors?.length || memory.audit?.warnings?.length) ? `<h2>Audit</h2>${jsonBlock(memory.audit)}` : ""}
      </section>`;
    }

    async function memoryAction(id, action, body) {
      await api(`/api/memory/${encodeURIComponent(id)}/${action}`, {method: "POST", body: body ? JSON.stringify(body) : undefined});
      setMessage(`${action} saved.`);
      await loadView(activeView);
    }

    async function reviewMemoryAction(id, action) {
      await memoryAction(id, action);
    }

    async function rejectMemory(id) {
      const reason = prompt("Reject reason");
      if (reason) {
        await memoryAction(id, "reject", {reason});
      }
    }

    async function reviewRejectMemory(id) {
      await rejectMemory(id);
    }

    function countBadges(counts) {
      const entries = Object.entries(counts || {});
      if (!entries.length) return `<span class="muted">None</span>`;
      return entries.map(([key, value]) => `<span class="badge">${escapeHtml(key)} ${escapeHtml(value)}</span>`).join(" ");
    }

    function sourceDocsList(documents, sourceIds = []) {
      const unresolved = (sourceIds || []).filter(sourceId => !String(sourceId).startsWith("document:") || !(documents || []).find(doc => doc.source_id === sourceId));
      const docs = (documents || []).map(doc => `<li><strong>${escapeHtml(doc.source_id)}</strong> ${escapeHtml(doc.title || "")}<br><span class="muted">${escapeHtml(doc.source_type || "")} ${escapeHtml(doc.source_path || "")}</span></li>`).join("");
      const missing = unresolved.map(sourceId => `<li><strong>${escapeHtml(sourceId)}</strong> <span class="muted">unresolved</span></li>`).join("");
      if (!docs && !missing) return `<div class="muted">No source evidence.</div>`;
      return `<ul class="source-list">${docs}${missing}</ul>`;
    }

    function extractHeadings(markdown) {
      const headings = [];
      for (const match of String(markdown || "").matchAll(/^##\\s+(.+?)\\s*$/gm)) {
        headings.push(match[1]);
      }
      return headings.length ? headings : ["Summary"];
    }

    function extractSectionBody(markdown, heading) {
      if (!heading) return "";
      const escaped = heading.replace(/[.*+?^${}()|[\\]\\\\]/g, "\\\\$&");
      const match = String(markdown || "").match(new RegExp(`^##\\\\s+${escaped}\\\\s*\\\\n([\\\\s\\\\S]*?)(?=^##\\\\s+|$)`, "m"));
      return match ? match[1].trim() : "";
    }

    function lineDiff(before, after) {
      const left = String(before || "").split("\\n");
      const right = String(after || "").split("\\n");
      if (left.length * right.length > 40000) {
        return `<pre class="diff"><div class="del">- ${escapeHtml(left.join("\\n- "))}</div><div class="add">+ ${escapeHtml(right.join("\\n+ "))}</div></pre>`;
      }
      const dp = Array.from({length: left.length + 1}, () => Array(right.length + 1).fill(0));
      for (let i = left.length - 1; i >= 0; i--) {
        for (let j = right.length - 1; j >= 0; j--) {
          dp[i][j] = left[i] === right[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
        }
      }
      const lines = [];
      let i = 0;
      let j = 0;
      while (i < left.length && j < right.length) {
        if (left[i] === right[j]) {
          lines.push(`<div class="ctx">  ${escapeHtml(left[i])}</div>`);
          i++;
          j++;
        } else if (dp[i + 1][j] >= dp[i][j + 1]) {
          lines.push(`<div class="del">- ${escapeHtml(left[i])}</div>`);
          i++;
        } else {
          lines.push(`<div class="add">+ ${escapeHtml(right[j])}</div>`);
          j++;
        }
      }
      while (i < left.length) {
        lines.push(`<div class="del">- ${escapeHtml(left[i++])}</div>`);
      }
      while (j < right.length) {
        lines.push(`<div class="add">+ ${escapeHtml(right[j++])}</div>`);
      }
      return `<pre class="diff">${lines.join("")}</pre>`;
    }

    function jsString(value) {
      return JSON.stringify(String(value ?? ""));
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
      if (name === "memory") return "Memory Review";
      if (name === "wiki") return "Wiki";
      if (name === "packets") return "Wiki Packets";
      if (name === "curation") return "Chief of Staff";
      if (name === "review") return "Review";
      return name.charAt(0).toUpperCase() + name.slice(1);
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
