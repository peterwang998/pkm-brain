from __future__ import annotations

import json
import os
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
from .cos_actions import recent_actions, row_to_action
from .cos_audit import COS_AUDIT_CONFIGURED_NOTE, COS_AUDIT_STUB_NOTE, run_sampled_audit
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
    create_confirmed_page_fact,
    managed_fact_page_review,
    reconcile_open_fact_questions,
    regenerate_managed_fact_page,
    revert_wiki_page_snapshot,
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
            elif path == "/api/wiki/facts/page":
                self.write_json(ui_wiki_fact_page_review(self.server.paths, query))
            elif path == "/api/wiki/facts":
                self.write_json(ui_wiki_fact_dashboard(self.server.paths))
            elif path == "/api/cos/policy":
                self.write_json(ui_cos_policy(self.server.paths))
            elif path == "/api/cos/actions":
                self.write_json(ui_cos_actions(self.server.paths, query))
            elif path == "/api/cos/review":
                self.write_json(ui_cos_review(self.server.paths))
            elif path == "/api/cos/contracts":
                self.write_json(ui_cos_contracts(self.server.paths))
            elif path == "/api/cos/audit":
                self.write_json(ui_cos_audit_status(self.server.paths))
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
            if parts[:2] == ["wiki", "questions"]:
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
        "related": list(frontmatter.get("related") or []),
    }


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


def ui_cos_review(paths: BrainPaths) -> dict[str, Any]:
    service(paths).init_workspace()
    with connection(paths.sqlite_path) as conn:
        policy_version = active_policy_version(conn)
        residue = [
            row_to_question(row)
            for row in conn.execute(
                """
                SELECT *
                FROM open_questions
                WHERE status IN ('open', 'needs_human')
                ORDER BY
                  CASE risk_tier WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END,
                  created_at DESC
                LIMIT 50
                """
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
        "counts": {
            "residue": len(residue),
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
    .callout {
      margin: .75rem 0;
      padding: .65rem .75rem;
      border: 1px solid #b7d8d2;
      border-left: 4px solid var(--accent);
      border-radius: 6px;
      background: #eefaf7;
    }
    .callout.warn {
      border-color: #e4c782;
      border-left-color: var(--warn);
      background: #fff8ea;
    }
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
    <button type="button" data-view="curation">Chief of Staff</button>
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
      curation: renderCuration,
      memory: renderMemory
    };
    let activeView = "status";
    let memoryStatus = "proposed";
    let selectedMemoryId = "";
    const wikiState = {q: "", type: "", status: "", selectedPath: ""};
    const curationState = {selectedQuestionId: "", selectedPagePath: "", lastResult: null};

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
          <div class="callout">
            <strong>Current retrieval path:</strong> raw sources are ingested into chunks, extracted into active facts, and rendered into managed wiki pages. <code>retrieve_context</code> searches active facts, raw source chunks, wiki pages, and memory records.
          </div>
          ${retrievalSurfacesHtml(data.retrieval_surfaces || [])}
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
      }
      const detail = wikiState.selectedPath ? await wikiPageDetailHtml(wikiState.selectedPath) : `<section><h2>Page</h2><div class="muted">Select a page.</div></section>`;
      app.innerHTML = `
        <section>
          <div class="toolbar">
            <h2>Wiki</h2>
            <span class="badge ok">searched by retrieval</span>
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
          <div class="callout">Wiki pages are searched as compiled page-level context. Managed pages are projections from active facts; reference pages and older semantic pages may still appear because they remain valid wiki files.</div>
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
    }

    function wikiPageRow(page) {
      const selected = page.relative_path === wikiState.selectedPath ? "ok" : "";
      return `<tr><td>${escapeHtml(wikiGroup(page))}</td><td><button class="row-button ${selected}" type="button" onclick='openWikiPage(${jsString(page.relative_path)})'>${escapeHtml(page.title)}</button><div class="muted">${escapeHtml(page.relative_path)}</div></td><td>${escapeHtml(page.page_type)}<br><span class="badge">${escapeHtml(page.status)}</span></td><td>${page.source_count}</td></tr>`;
    }

    function wikiGroup(page) {
      if (page.relative_path === "index.md" || page.relative_path === "log.md") return page.relative_path;
      return page.relative_path.split("/")[0] || page.page_type || "";
    }

    async function openWikiPage(path) {
      wikiState.selectedPath = path;
      await loadView("wiki");
    }

    async function wikiPageDetailHtml(path) {
      const page = await api(`/api/wiki/page?path=${encodeURIComponent(path)}`);
      return `<section>
        <div class="toolbar">
          <h2>${escapeHtml(page.frontmatter.title || page.relative_path)}</h2>
          <span class="badge">${page.generated ? "generated" : "hand-edited"}</span>
        </div>
        <div class="grid">
          ${metric("Type", page.frontmatter.page_type || "")}
          ${metric("Status", page.frontmatter.status || "")}
          ${metric("Updated", page.frontmatter.updated_at || "")}
          ${metric("Sources", page.source_ids?.length || 0)}
        </div>
        <h2>Source Evidence</h2>
        ${sourceDocsList(page.source_documents, page.source_ids)}
        <h2>Markdown</h2>
        <pre>${escapeHtml(page.markdown)}</pre>
      </section>`;
    }

    async function renderCuration() {
      const [data, review] = await Promise.all([
        api("/api/wiki/facts"),
        api("/api/cos/review")
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
      app.innerHTML = `
        <section>
          <div class="toolbar">
            <h2>Chief of Staff</h2>
            <span class="badge ok">fact layer</span>
            <button type="button" onclick="loadView('curation')">Refresh</button>
            <button class="primary" type="button" onclick="regenerateCurationPage(false)" ${selectedPage ? "" : "disabled"}>Regenerate Page</button>
            <button type="button" onclick="regenerateCurationPage(true)" ${selectedPage ? "" : "disabled"}>Preview Page</button>
            <button type="button" onclick="migrateExistingWikiFacts(true)">Preview Wiki Migration</button>
            <button type="button" onclick="migrateExistingWikiFacts(false)">Backfill Wiki Facts</button>
            <button type="button" onclick="reconcileChiefOfStaffQuestions()">Reconcile Duplicates</button>
          </div>
          <div class="callout">This is the active fact/page curation path. Retrieval can return searchable active facts directly, and managed wiki pages are regenerated projections of those facts.</div>
          <div class="grid">
            ${metric("Facts", data.counts?.facts ?? 0)}
            ${metric("Active", data.counts?.by_status?.active ?? 0, "ok")}
            ${metric("Managed Pages", pages.length)}
            ${metric("Conflicted", data.counts?.by_status?.conflicted ?? 0, data.counts?.by_status?.conflicted ? "danger" : "")}
            ${metric("Open Questions", data.counts?.questions_by_status?.open ?? 0, data.counts?.questions_by_status?.open ? "warn" : "ok")}
            ${metric("Policy", review.policy_version ?? "")}
            ${metric("Human Residue", review.counts?.residue ?? 0, review.counts?.residue ? "warn" : "ok")}
            ${metric("Audit Failures", review.counts?.audit_failures ?? 0, review.counts?.audit_failures ? "danger" : "ok")}
          </div>
          ${cosReviewHtml(review)}
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
          <h2>Recent Facts</h2>
          ${recentFactsTable(data.recent_facts || [])}
          <h2>Recent Curation Runs</h2>
          ${curationRunsTable(data.recent_runs || [])}
        </section>`;
    }

    function cosReviewHtml(review) {
      return `<div class="split">
        <section>
          <div class="toolbar">
            <h2>Human Residue</h2>
            <span class="badge">${review.counts?.residue ?? 0}</span>
          </div>
          ${cosResidueTable(review.residue || [])}
        </section>
        <section>
          <div class="toolbar">
            <h2>Action Health</h2>
            <span class="badge">${review.recent_auto_applied?.length || 0} recent</span>
            <span class="badge ${review.audit_failures?.length ? "danger" : "ok"}">${review.audit_failures?.length || 0} audit failures</span>
          </div>
          <h2>Recent Auto/Applied</h2>
          ${cosActionsTable(review.recent_auto_applied || [])}
          <h2>Audit Failures</h2>
          ${cosActionsTable(review.audit_failures || [])}
        </section>
      </div>`;
    }

    function cosResidueTable(residue) {
      return `<table><thead><tr><th>Kind</th><th>Question</th><th>Risk</th></tr></thead>
        <tbody>${residue.map(question => `<tr>
          <td><span class="badge ${question.kind === "conflict" ? "danger" : ""}">${escapeHtml(question.kind || "")}</span><br>${escapeHtml(question.status || "")}</td>
          <td>${escapeHtml(question.question || "")}<div class="muted">${escapeHtml(question.page_hint || question.action_id || "")}</div></td>
          <td>${escapeHtml(question.risk_tier || "")}<br>${escapeHtml(question.auto_resolve_after || "")}</td>
        </tr>`).join("") || emptyRow(3)}</tbody></table>`;
    }

    function cosActionsTable(actions) {
      return `<table><thead><tr><th>Action</th><th>Status</th><th>Targets</th></tr></thead>
        <tbody>${actions.map(action => `<tr>
          <td>${escapeHtml(action.action_type || "")}<div class="muted">${escapeHtml(action.id || "")}</div></td>
          <td><span class="badge ${action.audit_status === "sampled_bad" ? "danger" : action.status === "auto_applied" || action.status === "applied" ? "ok" : ""}">${escapeHtml(action.status || "")}</span><br>${escapeHtml(action.audit_status || "")}</td>
          <td>${escapeHtml(actionTargets(action))}<div class="muted">${escapeHtml(action.applied_at || action.created_at || "")}</div></td>
        </tr>`).join("") || emptyRow(3)}</tbody></table>`;
    }

    function actionTargets(action) {
      const pageTargets = action.target_page_paths || [];
      const factTargets = action.target_fact_ids || [];
      const contractTargets = action.target_contract_ids || [];
      const parts = [];
      if (pageTargets.length) parts.push(pageTargets.slice(0, 2).join(", "));
      if (factTargets.length) parts.push(`${factTargets.length} facts`);
      if (contractTargets.length) parts.push(`${contractTargets.length} contracts`);
      return parts.join(" / ");
    }

    function lastCurationResultHtml(result) {
      const pages = result.curation?.pages || [];
      return `<section>
        <div class="toolbar">
          <h2>Last Curation Output</h2>
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
          <button type="button" onclick='openWikiPage(${jsString(page.relative_path)})'>Open In Wiki</button>
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
      return `<table><thead><tr><th>Run</th><th>Source</th><th>Outcome</th><th>Created</th></tr></thead>
        <tbody>${runs.map(run => `<tr>
          <td>${escapeHtml(run.id)}</td>
          <td>${escapeHtml(run.source_packet_id || "")}<div class="muted">${escapeHtml(run.group_by || "")}</div></td>
          <td>${escapeHtml(run.status || "")}<br>${countBadges(run.summary || {})}</td>
          <td>${escapeHtml(run.created_at || "")}</td>
        </tr>`).join("") || emptyRow(4)}</tbody></table>`;
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

    async function rejectMemory(id) {
      const reason = prompt("Reject reason");
      if (reason) {
        await memoryAction(id, "reject", {reason});
      }
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

    function retrievalSurfacesHtml(surfaces) {
      return `<h2>Retrieval Surfaces</h2>
        <table><thead><tr><th>Surface</th><th>Searched</th><th>Rows</th><th>Role</th><th>Details</th></tr></thead>
        <tbody>${surfaces.map(surface => `<tr>
          <td>${escapeHtml(surface.surface || "")}</td>
          <td><span class="badge ${surface.searched ? "ok" : "warn"}">${surface.searched ? "searched" : "not searched"}</span></td>
          <td>${escapeHtml(String(surface.count ?? 0))}${surface.indexed !== undefined ? `<br><span class="muted">${escapeHtml(String(surface.indexed))} indexed</span>` : ""}</td>
          <td>${escapeHtml(surface.role || "")}</td>
          <td>${escapeHtml(surface.details || "")}</td>
        </tr>`).join("") || emptyRow(5)}</tbody></table>`;
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
      if (name === "curation") return "Chief of Staff";
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
