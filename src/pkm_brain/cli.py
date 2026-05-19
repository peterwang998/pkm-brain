from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from .audit import audit_memories, provenance_check
from .automation import (
    as_jsonable,
    install_launch_agent,
    install_nightly_launch_agent,
    index_status as automation_index_status,
    launch_agent_status,
    nightly_launch_agent_status,
    render_launch_agent,
    render_nightly_launch_agent,
    uninstall_launch_agent,
    uninstall_nightly_launch_agent,
    run_agent_log_ingest,
    run_nightly_maintenance,
    run_secondary_tick,
)
from .capture import AgentLogCapture
from .db import connection, rows
from .llm import provider_status
from .memory_proposals import propose_failure_memories_from_sources
from .mcp_server import create_mcp
from .paths import BrainPaths
from .service import BrainService
from .sync_connection import test_connection as run_sync_test_connection
from .sync_setup import add_peer as sync_add_peer_config
from .sync_setup import init_primary as sync_init_primary_config
from .sync_setup import init_secondary as sync_init_secondary_config
from .sync_ssh import first_host_key_with_fingerprint
from .sync_status import format_status_table_rows
from .sync_transfer import sync_pull as run_sync_pull
from .sync_transfer import sync_push as run_sync_push
from .sync_transfer import sync_run as run_sync_run
from .wiki import lint_wiki, synthesize_wiki
from .wiki_proposals import (
    apply_wiki_proposal,
    generate_interview_questions,
    inspect_wiki_proposal,
    list_wiki_proposals,
    propose_from_sources as propose_wiki_from_sources,
    record_wiki_interview,
    reject_wiki_proposal,
)

app = typer.Typer(help="Local personal knowledge management and agent memory tool.")
inspect_app = typer.Typer(help="Inspect documents and chunks.")
index_app = typer.Typer(help="Index health commands.")
wiki_app = typer.Typer(help="Wiki commands.")
wiki_proposals_app = typer.Typer(help="Wiki proposal review commands.")
memory_app = typer.Typer(help="Typed memory commands.")
llm_app = typer.Typer(help="LLM provider commands.")
runs_app = typer.Typer(help="Pipeline run commands.")
provenance_app = typer.Typer(help="Provenance validation commands.")
capture_app = typer.Typer(help="Capture external sources into the inbox.")
automation_app = typer.Typer(help="Scheduled automation commands.")
launch_agent_app = typer.Typer(help="macOS LaunchAgent commands.")
sync_app = typer.Typer(help="Primary/Secondary sync commands.")
scheduler_app = typer.Typer(help="Logical scheduler commands.")
app.add_typer(inspect_app, name="inspect")
app.add_typer(index_app, name="index")
app.add_typer(wiki_app, name="wiki")
wiki_app.add_typer(wiki_proposals_app, name="proposals")
app.add_typer(memory_app, name="memory")
app.add_typer(llm_app, name="llm")
app.add_typer(runs_app, name="runs")
app.add_typer(provenance_app, name="provenance")
app.add_typer(capture_app, name="capture")
app.add_typer(automation_app, name="automation")
app.add_typer(launch_agent_app, name="launch-agent")
app.add_typer(sync_app, name="sync")
app.add_typer(scheduler_app, name="scheduler")
console = Console()


def service(home: Optional[Path] = None) -> BrainService:
    return BrainService(BrainPaths.from_value(home))


def required_or_prompt(value: Optional[str], flag: str, prompt: str, yes: bool) -> str:
    if value and value.strip():
        return value.strip()
    if yes:
        raise ValueError(f"{flag} is required with --yes")
    return str(typer.prompt(prompt)).strip()


@app.command()
def init(home: Optional[Path] = typer.Option(None, help="Brain home directory.")) -> None:
    svc = service(home)
    svc.init_workspace()
    console.print(f"Initialized brain workspace at [bold]{svc.paths.home}[/bold]")


@app.command()
def doctor(home: Optional[Path] = typer.Option(None), json_output: bool = typer.Option(False, "--json")) -> None:
    status = service(home).doctor()
    if json_output:
        console.print_json(json.dumps(status))
        return
    table = Table(title="Brain Doctor")
    table.add_column("Check")
    table.add_column("Status")
    table.add_column("Detail")
    table.add_row("home", "ok", status["home"])
    table.add_row("sqlite", "ok" if status["sqlite"] else "missing", str(status["sqlite"]))
    table.add_row("lancedb", "ok" if status["lancedb"] else "missing", str(status["lancedb"]))
    table.add_row("embedding_provider", "ok", status["embedding_provider"])
    for name, exists in status["directories"].items():
        table.add_row(f"directory:{name}", "ok" if exists else "missing", str(exists))
    console.print(table)


@app.command()
def ingest(
    path: Optional[Path] = typer.Argument(None, help="Path to ingest. Defaults to inbox."),
    dry_run: bool = typer.Option(False, "--dry-run"),
    retry_quarantine: bool = typer.Option(False, "--retry-quarantine", help="Restore and retry files in external inbox quarantine."),
    home: Optional[Path] = typer.Option(None),
) -> None:
    result = service(home).ingest(path, dry_run=dry_run, retry_quarantine=retry_quarantine)
    console.print_json(json.dumps(result.as_dict()))


@app.command()
def search(
    query: str,
    limit: int = typer.Option(10),
    debug: bool = typer.Option(False, "--debug"),
    home: Optional[Path] = typer.Option(None),
) -> None:
    result = service(home).search(query, limit=limit, debug=debug)
    print_search_result(result)


@app.command("retrieve-context")
def retrieve_context(
    task: str = typer.Option(...),
    project: Optional[str] = typer.Option(None),
    budget: Optional[int] = typer.Option(None, help="Override the selected retrieval mode's token budget."),
    mode: str = typer.Option("default", help="Retrieval mode: compact, default, broad, or inspect."),
    debug: bool = typer.Option(False, "--debug"),
    home: Optional[Path] = typer.Option(None),
) -> None:
    result = service(home).retrieve_context(task, project=project, budget=budget, mode=mode, debug=debug)
    console.print_json(json.dumps(result))


@inspect_app.command("document")
def inspect_document(document_id: str, home: Optional[Path] = typer.Option(None)) -> None:
    svc = service(home)
    svc.init_workspace()
    with connection(svc.paths.sqlite_path) as conn:
        row = conn.execute("SELECT * FROM documents WHERE id = ?", (document_id,)).fetchone()
    if not row:
        console.print(f"Document not found: {document_id}")
        raise typer.Exit(1)
    console.print_json(json.dumps(dict(row)))


@inspect_app.command("chunks")
def inspect_chunks(document_id: str, home: Optional[Path] = typer.Option(None)) -> None:
    svc = service(home)
    svc.init_workspace()
    with connection(svc.paths.sqlite_path) as conn:
        found = rows(conn, "SELECT * FROM chunks WHERE document_id = ? ORDER BY chunk_index", (document_id,))
    table = Table(title=f"Chunks for {document_id}")
    for col in ["chunk_index", "id", "heading_path", "token_count", "preview"]:
        table.add_column(col)
    for row in found:
        table.add_row(
            str(row["chunk_index"]),
            row["id"],
            row["heading_path"] or "",
            str(row["token_count"]),
            row["text"][:120].replace("\n", " "),
        )
    console.print(table)


@index_app.command("status")
def index_status(home: Optional[Path] = typer.Option(None)) -> None:
    svc = service(home)
    svc.init_workspace()
    console.print_json(json.dumps(automation_index_status(svc.paths, svc)))


@wiki_app.command("lint")
def wiki_lint(home: Optional[Path] = typer.Option(None)) -> None:
    svc = service(home)
    svc.init_workspace()
    result = lint_wiki(svc.paths)
    console.print_json(json.dumps(result))
    if result["errors"]:
        raise typer.Exit(1)


@wiki_app.command("synthesize")
def wiki_synthesize(
    dry_run: bool = typer.Option(False, "--dry-run"),
    overwrite_generated: bool = typer.Option(False, "--overwrite-generated"),
    home: Optional[Path] = typer.Option(None),
) -> None:
    svc = service(home)
    svc.init_workspace()
    result = synthesize_wiki(svc.paths, dry_run=dry_run, overwrite_generated=overwrite_generated)
    console.print_json(json.dumps(result))
    lint_result = result.get("lint")
    if lint_result and lint_result["errors"]:
        raise typer.Exit(1)


@wiki_proposals_app.command("list")
def wiki_proposals_list(
    status: Optional[str] = typer.Option(None),
    home: Optional[Path] = typer.Option(None),
) -> None:
    svc = service(home)
    svc.init_workspace()
    console.print_json(json.dumps(list_wiki_proposals(svc.paths, status=status)))


@wiki_proposals_app.command("inspect")
def wiki_proposals_inspect(batch_id: str, home: Optional[Path] = typer.Option(None)) -> None:
    svc = service(home)
    svc.init_workspace()
    console.print_json(json.dumps(inspect_wiki_proposal(svc.paths, batch_id)))


@wiki_proposals_app.command("reject")
def wiki_proposals_reject(
    batch_id: str,
    reason: Optional[str] = typer.Option(None),
    home: Optional[Path] = typer.Option(None),
) -> None:
    svc = service(home)
    svc.init_workspace()
    console.print_json(json.dumps(reject_wiki_proposal(svc.paths, batch_id, reason=reason)))


@wiki_app.command("interview")
def wiki_interview(
    batch_id: str,
    provider: Optional[str] = typer.Option(None),
    home: Optional[Path] = typer.Option(None),
) -> None:
    svc = service(home)
    svc.init_workspace()
    generated = generate_interview_questions(svc.paths, batch_id, provider_name=provider)
    questions = generated["questions"]
    answers: list[str] = []
    for question in questions:
        answers.append(typer.prompt(question, default=""))
    disposition = typer.prompt("Disposition: approved, rejected, or needs_interview", default="needs_interview")
    result = record_wiki_interview(
        svc.paths,
        batch_id,
        questions,
        answers,
        disposition,
        provider=generated.get("provider"),
        model=generated.get("model"),
    )
    console.print_json(json.dumps(result))


@wiki_app.command("apply")
def wiki_apply(batch_id: str, home: Optional[Path] = typer.Option(None)) -> None:
    svc = service(home)
    svc.init_workspace()
    result = apply_wiki_proposal(svc.paths, batch_id)
    console.print_json(json.dumps(result))
    if result["lint"]["errors"]:
        raise typer.Exit(1)


@wiki_app.command("propose-from-sources")
def wiki_propose_from_sources(
    provider: Optional[str] = typer.Option(None),
    limit: int = typer.Option(8),
    home: Optional[Path] = typer.Option(None),
) -> None:
    svc = service(home)
    svc.init_workspace()
    result = propose_wiki_from_sources(svc.paths, provider_name=provider, limit=limit)
    console.print_json(json.dumps(result))


@llm_app.command("doctor")
def llm_doctor(provider: Optional[str] = typer.Option(None)) -> None:
    console.print_json(json.dumps(provider_status(provider)))


@memory_app.command("propose")
def memory_propose(
    memory_type: str,
    scope: str,
    content: str,
    source: list[str] = typer.Option([], "--source"),
    confidence: float = typer.Option(0.8),
    home: Optional[Path] = typer.Option(None),
) -> None:
    memory_id = service(home).propose_memory(memory_type, scope, content, source, confidence)
    console.print_json(json.dumps({"memory_id": memory_id, "status": "proposed"}))


@memory_app.command("list")
def memory_list(status: Optional[str] = typer.Option(None), home: Optional[Path] = typer.Option(None)) -> None:
    svc = service(home)
    console.print_json(json.dumps(svc.list_memories(status=status)))


@memory_app.command("inspect")
def memory_inspect(memory_id: str, home: Optional[Path] = typer.Option(None)) -> None:
    svc = service(home)
    try:
        row = svc.get_memory(memory_id)
    except ValueError:
        console.print(f"Memory not found: {memory_id}")
        raise typer.Exit(1)
    console.print_json(json.dumps(row))


@memory_app.command("approve")
def memory_approve(memory_id: str, home: Optional[Path] = typer.Option(None)) -> None:
    try:
        result = service(home).approve_memory(memory_id)
    except ValueError as exc:
        console.print(str(exc))
        raise typer.Exit(1)
    console.print_json(json.dumps(result))


@memory_app.command("reject")
def memory_reject(
    memory_id: str,
    reason: str = typer.Option(..., "--reason", help="Reason for rejecting the proposed memory."),
    home: Optional[Path] = typer.Option(None),
) -> None:
    try:
        result = service(home).reject_memory(memory_id, reason)
    except ValueError as exc:
        console.print(str(exc))
        raise typer.Exit(1)
    console.print_json(json.dumps(result))


@memory_app.command("archive")
def memory_archive(memory_id: str, home: Optional[Path] = typer.Option(None)) -> None:
    try:
        result = service(home).archive_memory(memory_id)
    except ValueError as exc:
        console.print(str(exc))
        raise typer.Exit(1)
    console.print_json(json.dumps(result))


@memory_app.command("export-all")
def memory_export_all(home: Optional[Path] = typer.Option(None)) -> None:
    result = service(home).export_all_memories()
    console.print_json(json.dumps(result))


@memory_app.command("import")
def memory_import(
    from_dir: Path = typer.Option(..., "--from", help="Directory containing memory markdown exports."),
    allow_missing_sources: bool = typer.Option(False, "--allow-missing-sources"),
    home: Optional[Path] = typer.Option(None),
) -> None:
    try:
        result = service(home).import_memories(from_dir, allow_missing_sources=allow_missing_sources)
    except ValueError as exc:
        console.print(str(exc))
        raise typer.Exit(1)
    console.print_json(json.dumps(result))
    if result["errors"]:
        raise typer.Exit(1)


@memory_app.command("propose-from-sources")
def memory_propose_from_sources(
    provider: Optional[str] = typer.Option("codex"),
    limit: int = typer.Option(12),
    home: Optional[Path] = typer.Option(None),
) -> None:
    svc = service(home)
    svc.init_workspace()
    result = propose_failure_memories_from_sources(svc.paths, provider_name=provider, limit=limit)
    console.print_json(json.dumps(result))


@memory_app.command("audit")
def memory_audit(home: Optional[Path] = typer.Option(None)) -> None:
    svc = service(home)
    svc.init_workspace()
    result = audit_memories(svc.paths)
    console.print_json(json.dumps(result))
    if result["errors"]:
        raise typer.Exit(1)


@provenance_app.command("check")
def provenance_check_command(home: Optional[Path] = typer.Option(None)) -> None:
    svc = service(home)
    svc.init_workspace()
    result = provenance_check(svc.paths)
    console.print_json(json.dumps(result))
    if result["errors"]:
        raise typer.Exit(1)


@runs_app.command("list")
def runs_list(home: Optional[Path] = typer.Option(None)) -> None:
    svc = service(home)
    svc.init_workspace()
    with connection(svc.paths.sqlite_path) as conn:
        found = [dict(row) for row in rows(conn, "SELECT * FROM ingestion_runs ORDER BY started_at DESC LIMIT 20")]
    console.print_json(json.dumps(found))


@runs_app.command("inspect")
def runs_inspect(run_id: str, home: Optional[Path] = typer.Option(None)) -> None:
    svc = service(home)
    svc.init_workspace()
    with connection(svc.paths.sqlite_path) as conn:
        row = conn.execute("SELECT * FROM ingestion_runs WHERE id = ?", (run_id,)).fetchone()
    if not row:
        console.print(f"Run not found: {run_id}")
        raise typer.Exit(1)
    console.print_json(json.dumps(dict(row)))


@sync_app.command("doctor")
def sync_doctor(
    home: Optional[Path] = typer.Option(None),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    svc = BrainService(BrainPaths.from_value(home), prefer_model_embeddings=False)
    result = svc.sync_doctor()
    if json_output:
        console.print_json(json.dumps(result))
    else:
        table = Table(title="Sync Doctor")
        table.add_column("Check")
        table.add_column("Status")
        table.add_column("Message")
        for check in result["checks"]:
            table.add_row(check["name"], check["status"], check["message"])
        console.print(f"Role: {result['role'] or 'unknown'}")
        console.print(f"Node: {result['node_id'] or 'unknown'}")
        console.print(table)
        console.print(f"Ready: {'yes' if result['ready'] else 'no'}")
    if not result["ready"]:
        raise typer.Exit(1)


@sync_app.command("init-primary")
def sync_init_primary(
    node_id: Optional[str] = typer.Option(None, "--node-id"),
    yes: bool = typer.Option(False, "--yes", help="Run non-interactively; required fields must be provided."),
    force: bool = typer.Option(False, "--force", help="Overwrite an existing sync.yaml."),
    home: Optional[Path] = typer.Option(None),
) -> None:
    paths = BrainPaths.from_value(home)
    try:
        resolved_node_id = required_or_prompt(node_id, "--node-id", "Primary node_id", yes)
        result = sync_init_primary_config(paths, resolved_node_id, force=force)
    except ValueError as exc:
        console.print(str(exc))
        raise typer.Exit(1)
    console.print_json(json.dumps(result))


@sync_app.command("init-secondary")
def sync_init_secondary(
    node_id: Optional[str] = typer.Option(None, "--node-id"),
    primary_node_id: Optional[str] = typer.Option(None, "--primary-node-id"),
    outbox_path: Optional[Path] = typer.Option(None, "--outbox-path"),
    yes: bool = typer.Option(False, "--yes", help="Run non-interactively; required fields must be provided."),
    force: bool = typer.Option(False, "--force", help="Overwrite an existing sync.yaml."),
    home: Optional[Path] = typer.Option(None),
) -> None:
    paths = BrainPaths.from_value(home)
    try:
        resolved_node_id = required_or_prompt(node_id, "--node-id", "Secondary node_id", yes)
        resolved_primary_node_id = required_or_prompt(primary_node_id, "--primary-node-id", "Expected Primary node_id", yes)
        result = sync_init_secondary_config(
            paths,
            resolved_node_id,
            resolved_primary_node_id,
            outbox_path=outbox_path,
            force=force,
        )
    except ValueError as exc:
        console.print(str(exc))
        raise typer.Exit(1)
    console.print_json(json.dumps(result))


@sync_app.command("add-peer")
def sync_add_peer(
    node_id: Optional[str] = typer.Option(None, "--node-id"),
    host: Optional[str] = typer.Option(None, "--host"),
    user: Optional[str] = typer.Option(None, "--user"),
    brain_home: Optional[Path] = typer.Option(None, "--brain-home"),
    outbox_path: Optional[Path] = typer.Option(None, "--outbox-path", help="Remote outbox path when not <brain-home>/outbox/<node-id>."),
    identity_path: Optional[Path] = typer.Option(None, "--identity-path"),
    allow_first_host_key: bool = typer.Option(False, "--allow-first-host-key"),
    test_connection_now: bool = typer.Option(False, "--test-connection", help="Run test-connection after adding the peer."),
    yes: bool = typer.Option(False, "--yes", help="Run non-interactively; required fields must be provided."),
    home: Optional[Path] = typer.Option(None),
) -> None:
    paths = BrainPaths.from_value(home)
    try:
        resolved_node_id = required_or_prompt(node_id, "--node-id", "Secondary node_id", yes)
        resolved_host = required_or_prompt(host, "--host", "SSH host", yes)
        resolved_user = required_or_prompt(user, "--user", "SSH user", yes)
        resolved_brain_home = Path(required_or_prompt(str(brain_home) if brain_home else None, "--brain-home", "Remote Brain home", yes))
        host_key_candidate = None
        if allow_first_host_key:
            candidate, observed_fingerprint = first_host_key_with_fingerprint(resolved_host)
            console.print(f"Observed host key fingerprint for {resolved_host}: {observed_fingerprint}")
            if not yes and not typer.confirm("Accept this host key fingerprint after out-of-band verification?"):
                raise ValueError("host key was not accepted")
            host_key_candidate = candidate
        result = sync_add_peer_config(
            paths,
            resolved_node_id,
            resolved_host,
            resolved_user,
            resolved_brain_home,
            outbox_path=outbox_path,
            identity_path=identity_path,
            host_key_candidate=host_key_candidate,
        )
        should_test = test_connection_now
        if not yes and not should_test:
            should_test = typer.confirm("Test connection now?", default=False)
        if should_test:
            result["connection_test"] = run_sync_test_connection(paths, resolved_node_id).as_dict()
    except (RuntimeError, ValueError) as exc:
        console.print(str(exc))
        raise typer.Exit(1)
    console.print_json(json.dumps(result))


@sync_app.command("test-connection")
def sync_test_connection(
    peer_node_id: str,
    home: Optional[Path] = typer.Option(None),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    paths = BrainPaths.from_value(home)
    try:
        result = run_sync_test_connection(paths, peer_node_id).as_dict()
    except (RuntimeError, ValueError) as exc:
        console.print(str(exc))
        raise typer.Exit(1)
    if json_output:
        console.print_json(json.dumps(result))
    else:
        table = Table(title=f"Connection Test: {peer_node_id}")
        table.add_column("Check")
        table.add_column("Status")
        for check, status in result["checks"].items():
            table.add_row(check, status)
        console.print(f"Role: {result['local_role']}")
        console.print(f"Node: {result['local_node_id']}")
        console.print(f"Peer: {result['peer_node_id']}")
        console.print(table)
        console.print(f"Ready for scheduled sync: {'yes' if result['ready'] else 'no'}")
    if not result["ready"]:
        raise typer.Exit(1)


@sync_app.command("pull")
def sync_pull(
    peer_node_id: str,
    home: Optional[Path] = typer.Option(None),
) -> None:
    try:
        result = run_sync_pull(BrainPaths.from_value(home), peer_node_id).as_dict()
    except ValueError as exc:
        console.print(str(exc))
        raise typer.Exit(1)
    console.print_json(json.dumps(result))
    if result["status"] == "failed":
        raise typer.Exit(1)


@sync_app.command("push")
def sync_push(
    peer_node_id: str,
    home: Optional[Path] = typer.Option(None),
) -> None:
    try:
        result = run_sync_push(BrainPaths.from_value(home), peer_node_id).as_dict()
    except ValueError as exc:
        console.print(str(exc))
        raise typer.Exit(1)
    console.print_json(json.dumps(result))
    if result["status"] == "failed":
        raise typer.Exit(1)


@sync_app.command("run")
def sync_run(
    peer_node_id: str,
    if_reachable: bool = typer.Option(False, "--if-reachable", help="Skip cleanly when the peer is unreachable."),
    no_remote_ingest: bool = typer.Option(False, "--no-remote-ingest", help="Skip remote ingest after a successful push."),
    home: Optional[Path] = typer.Option(None),
) -> None:
    try:
        result = run_sync_run(
            BrainPaths.from_value(home),
            peer_node_id,
            remote_ingest=not no_remote_ingest,
            if_reachable=if_reachable,
        ).as_dict()
    except ValueError as exc:
        console.print(str(exc))
        raise typer.Exit(1)
    console.print_json(json.dumps(result))
    if result["status"] == "failed":
        raise typer.Exit(1)


@sync_app.command("status")
def sync_status(
    home: Optional[Path] = typer.Option(None),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    result = service(home).sync_status()
    if json_output:
        console.print_json(json.dumps(result))
        return
    if not result["configured"]:
        console.print("Sync is not configured.")
        for warning in result.get("warnings", []):
            console.print(f"warning: {warning}")
        return
    table = Table(title="Sync Status")
    for column in ["Peer", "Last Pull", "Last Push", "Last Failure", "Mirror Current"]:
        table.add_column(column)
    for row in format_status_table_rows(result):
        table.add_row(*row)
    console.print(f"Role: {result['role']}")
    console.print(f"Node: {result['node_id']}")
    console.print(table)
    for warning in result.get("warnings", []):
        console.print(f"warning: {warning}")


@sync_app.command("conflicts")
def sync_conflicts(
    home: Optional[Path] = typer.Option(None),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    result = service(home).sync_conflicts()
    if json_output:
        console.print_json(json.dumps(result))
        return
    table = Table(title="Sync Conflicts")
    for column in ["Logical Source", "Origins", "Documents"]:
        table.add_column(column)
    for conflict in result["conflicts"]:
        table.add_row(
            conflict["logical_source_key"],
            ", ".join(conflict["origins"]),
            ", ".join(conflict["document_ids"]),
        )
    console.print(table)
    console.print(f"Conflicts: {result['count']}")


@app.command()
def mcp(home: Optional[Path] = typer.Option(None)) -> None:
    create_mcp(str(home) if home else None).run()


@capture_app.command("agents")
def capture_agents(
    agent: str = typer.Option("all", help="Source to capture: all, codex, claude, opencode, or hyprnote."),
    include_hyprnote: bool = typer.Option(False, "--include-hyprnote", help="Include Hyprnote when --agent all is used."),
    hyprnote_root: Optional[Path] = typer.Option(None, help="Hyprnote root directory."),
    dry_run: bool = typer.Option(False, "--dry-run"),
    export_outbox: bool = typer.Option(False, "--export-outbox", help="Export captured files into this node's sync outbox."),
    also_ingest: bool = typer.Option(False, "--also-ingest", help="Run local ingest after capture."),
    home: Optional[Path] = typer.Option(None),
) -> None:
    svc = service(home)
    svc.init_workspace()
    result = AgentLogCapture(svc.paths, hyprnote_root=hyprnote_root, include_hyprnote=include_hyprnote).capture(
        agent=agent,
        dry_run=dry_run,
        export_outbox=export_outbox,
    )
    if also_ingest and not dry_run:
        ingest_result = svc.ingest()
        console.print_json(json.dumps({"capture": result.as_dict(), "ingest": ingest_result.as_dict()}))
        return
    console.print_json(json.dumps(result.as_dict()))


@automation_app.command("run-agent-log-ingest")
def automation_run_agent_log_ingest(
    agent: str = typer.Option("all", help="Source to capture: all, codex, claude, opencode, or hyprnote."),
    include_hyprnote: bool = typer.Option(False, "--include-hyprnote", help="Include Hyprnote when --agent all is used."),
    hyprnote_root: Optional[Path] = typer.Option(None, help="Hyprnote root directory."),
    home: Optional[Path] = typer.Option(None),
) -> None:
    paths = BrainPaths.from_value(home)
    result = run_agent_log_ingest(paths, agent=agent, hyprnote_root=hyprnote_root, include_hyprnote=include_hyprnote)
    console.print_json(json.dumps(as_jsonable(result)))


@automation_app.command("secondary-tick")
def automation_secondary_tick(
    agent: str = typer.Option("all", help="Source to capture: all, codex, claude, opencode, or hyprnote."),
    include_hyprnote: bool = typer.Option(False, "--include-hyprnote", help="Include Hyprnote when --agent all is used."),
    hyprnote_root: Optional[Path] = typer.Option(None, help="Hyprnote root directory."),
    home: Optional[Path] = typer.Option(None),
) -> None:
    paths = BrainPaths.from_value(home)
    result = run_secondary_tick(paths, agent=agent, hyprnote_root=hyprnote_root, include_hyprnote=include_hyprnote)
    console.print_json(json.dumps(as_jsonable(result)))


@automation_app.command("nightly")
def automation_nightly(
    if_due: bool = typer.Option(False, "--if-due", help="Skip when the last successful nightly run is still recent."),
    due_after_hours: int = typer.Option(20, help="Minimum hours between successful nightly runs when --if-due is set."),
    agent: str = typer.Option("all", help="Source to capture: all, codex, claude, opencode, or hyprnote."),
    include_hyprnote: bool = typer.Option(False, "--include-hyprnote", help="Include Hyprnote when --agent all is used."),
    hyprnote_root: Optional[Path] = typer.Option(None, help="Hyprnote root directory."),
    with_llm_wiki_proposals: bool = typer.Option(False, "--with-llm-wiki-proposals"),
    with_llm_memory_proposals: bool = typer.Option(False, "--with-llm-memory-proposals"),
    provider: Optional[str] = typer.Option(None),
    home: Optional[Path] = typer.Option(None),
) -> None:
    paths = BrainPaths.from_value(home)
    result = run_nightly_maintenance(
        paths,
        if_due=if_due,
        due_after_hours=due_after_hours,
        agent=agent,
        hyprnote_root=hyprnote_root,
        include_hyprnote=include_hyprnote,
        with_llm_wiki_proposals=with_llm_wiki_proposals,
        with_llm_memory_proposals=with_llm_memory_proposals,
        provider=provider,
    )
    console.print_json(json.dumps(as_jsonable(result)))
    if result.status == "failed":
        raise typer.Exit(1)


@scheduler_app.command("install-sync")
def scheduler_install_sync(
    peer: str = typer.Option(..., "--peer", help="Secondary peer node_id."),
    interval: int = typer.Option(1800, help="Polling interval in seconds."),
    dry_run: bool = typer.Option(False, "--dry-run"),
    home: Optional[Path] = typer.Option(None),
) -> None:
    from .scheduler.launchd import LaunchdScheduler, sync_primary_job

    paths = BrainPaths.from_value(home)
    BrainService(paths).init_workspace()
    uv = shutil.which("uv") or "/opt/homebrew/bin/uv"
    job = sync_primary_job(Path.cwd(), paths.home, Path(uv), peer, interval=interval)
    result = LaunchdScheduler().install(job, dry_run=dry_run)
    console.print_json(json.dumps(result))


@scheduler_app.command("install-secondary-capture")
def scheduler_install_secondary_capture(
    interval: int = typer.Option(600, help="Polling interval in seconds."),
    dry_run: bool = typer.Option(False, "--dry-run"),
    home: Optional[Path] = typer.Option(None),
) -> None:
    from .scheduler.launchd import LaunchdScheduler, secondary_capture_job

    paths = BrainPaths.from_value(home)
    BrainService(paths).init_workspace()
    uv = shutil.which("uv") or "/opt/homebrew/bin/uv"
    job = secondary_capture_job(Path.cwd(), paths.home, Path(uv), interval=interval)
    result = LaunchdScheduler().install(job, dry_run=dry_run)
    console.print_json(json.dumps(result))


@scheduler_app.command("status")
def scheduler_status() -> None:
    from .scheduler.launchd import LaunchdScheduler

    result = [status.as_dict() for status in LaunchdScheduler().status()]
    console.print_json(json.dumps(result))


@scheduler_app.command("uninstall-sync")
def scheduler_uninstall_sync(peer: str = typer.Option(..., "--peer", help="Secondary peer node_id.")) -> None:
    from .scheduler.launchd import LaunchdScheduler, SYNC_PRIMARY_LABEL

    _ = peer
    console.print_json(json.dumps(LaunchdScheduler().uninstall(SYNC_PRIMARY_LABEL)))


@scheduler_app.command("uninstall-secondary-capture")
def scheduler_uninstall_secondary_capture() -> None:
    from .scheduler.launchd import CAPTURE_SECONDARY_LABEL, LaunchdScheduler

    console.print_json(json.dumps(LaunchdScheduler().uninstall(CAPTURE_SECONDARY_LABEL)))


@launch_agent_app.command("install")
def launch_agent_install(
    interval: int = typer.Option(600, help="Polling interval in seconds."),
    include_hyprnote: bool = typer.Option(False, "--include-hyprnote", help="Include Hyprnote in scheduled capture."),
    dry_run: bool = typer.Option(False, "--dry-run"),
    home: Optional[Path] = typer.Option(None),
) -> None:
    paths = BrainPaths.from_value(home)
    BrainService(paths).init_workspace()
    uv = shutil.which("uv") or "/opt/homebrew/bin/uv"
    result = install_launch_agent(
        repo_path=Path.cwd(),
        brain_home=paths.home,
        uv_path=Path(uv),
        interval=interval,
        include_hyprnote=include_hyprnote,
        dry_run=dry_run,
    )
    console.print_json(json.dumps(result))


@launch_agent_app.command("status")
def launch_agent_status_command() -> None:
    console.print_json(json.dumps(launch_agent_status()))


@launch_agent_app.command("uninstall")
def launch_agent_uninstall() -> None:
    console.print_json(json.dumps(uninstall_launch_agent()))


@launch_agent_app.command("render")
def launch_agent_render(
    interval: int = typer.Option(600),
    include_hyprnote: bool = typer.Option(False, "--include-hyprnote"),
    home: Optional[Path] = typer.Option(None),
) -> None:
    paths = BrainPaths.from_value(home)
    uv = shutil.which("uv") or "/opt/homebrew/bin/uv"
    plist = render_launch_agent(Path.cwd(), paths.home, Path(uv), interval=interval, include_hyprnote=include_hyprnote)
    console.print_json(json.dumps(plist))


@launch_agent_app.command("install-nightly")
def launch_agent_install_nightly(
    interval: int = typer.Option(3600, help="Wake-check interval in seconds."),
    due_after_hours: int = typer.Option(20, help="Minimum hours between successful nightly runs."),
    with_llm_wiki_proposals: bool = typer.Option(False, "--with-llm-wiki-proposals"),
    with_llm_memory_proposals: bool = typer.Option(False, "--with-llm-memory-proposals"),
    provider: Optional[str] = typer.Option(None, help="LLM provider for proposals: codex, openai, anthropic, or ollama."),
    dry_run: bool = typer.Option(False, "--dry-run"),
    home: Optional[Path] = typer.Option(None),
) -> None:
    paths = BrainPaths.from_value(home)
    BrainService(paths).init_workspace()
    uv = shutil.which("uv") or "/opt/homebrew/bin/uv"
    result = install_nightly_launch_agent(
        repo_path=Path.cwd(),
        brain_home=paths.home,
        uv_path=Path(uv),
        interval=interval,
        due_after_hours=due_after_hours,
        with_llm_wiki_proposals=with_llm_wiki_proposals,
        with_llm_memory_proposals=with_llm_memory_proposals,
        provider=provider,
        dry_run=dry_run,
    )
    console.print_json(json.dumps(result))


@launch_agent_app.command("nightly-status")
def launch_agent_nightly_status_command() -> None:
    console.print_json(json.dumps(nightly_launch_agent_status()))


@launch_agent_app.command("uninstall-nightly")
def launch_agent_uninstall_nightly() -> None:
    console.print_json(json.dumps(uninstall_nightly_launch_agent()))


@launch_agent_app.command("render-nightly")
def launch_agent_render_nightly(
    interval: int = typer.Option(3600),
    due_after_hours: int = typer.Option(20),
    with_llm_wiki_proposals: bool = typer.Option(False, "--with-llm-wiki-proposals"),
    with_llm_memory_proposals: bool = typer.Option(False, "--with-llm-memory-proposals"),
    provider: Optional[str] = typer.Option(None, help="LLM provider for proposals: codex, openai, anthropic, or ollama."),
    home: Optional[Path] = typer.Option(None),
) -> None:
    paths = BrainPaths.from_value(home)
    uv = shutil.which("uv") or "/opt/homebrew/bin/uv"
    plist = render_nightly_launch_agent(
        Path.cwd(),
        paths.home,
        Path(uv),
        interval=interval,
        due_after_hours=due_after_hours,
        with_llm_wiki_proposals=with_llm_wiki_proposals,
        with_llm_memory_proposals=with_llm_memory_proposals,
        provider=provider,
    )
    console.print_json(json.dumps(plist))


def print_search_result(result: dict) -> None:
    table = Table(title=f"Search: {result['query']}")
    for col in ["rank", "chunk_id", "title", "source", "preview"]:
        table.add_column(col)
    for index, row in enumerate(result["results"], start=1):
        table.add_row(
            str(index),
            row["chunk_id"],
            row["title"],
            row["source_path"],
            row["text"][:160].replace("\n", " "),
        )
    console.print(table)
    console.print(f"retrieval_event_id: {result['event_id']}")
    if result.get("debug"):
        console.print_json(json.dumps(result["debug"]))
