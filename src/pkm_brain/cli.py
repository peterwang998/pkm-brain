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
)
from .capture import AgentLogCapture
from .db import connection, rows
from .llm import provider_status
from .mcp_server import create_mcp
from .paths import BrainPaths
from .service import BrainService
from .wiki import lint_wiki, synthesize_wiki
from .wiki_proposals import (
    apply_wiki_proposal,
    generate_interview_questions,
    inspect_wiki_proposal,
    list_wiki_proposals,
    propose_from_sources,
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
console = Console()


def service(home: Optional[Path] = None) -> BrainService:
    return BrainService(BrainPaths.from_value(home))


@app.command()
def init(home: Optional[Path] = typer.Option(None, help="Brain home directory.")) -> None:
    svc = service(home)
    svc.init_workspace()
    console.print(f"Initialized brain workspace at [bold]{svc.paths.home}[/bold]")


@app.command()
def doctor(home: Optional[Path] = typer.Option(None)) -> None:
    status = service(home).doctor()
    console.print_json(json.dumps(status))


@app.command()
def ingest(
    path: Optional[Path] = typer.Argument(None, help="Path to ingest. Defaults to inbox."),
    dry_run: bool = typer.Option(False, "--dry-run"),
    home: Optional[Path] = typer.Option(None),
) -> None:
    result = service(home).ingest(path, dry_run=dry_run)
    console.print_json(json.dumps(result.__dict__))


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
    budget: int = typer.Option(8000),
    home: Optional[Path] = typer.Option(None),
) -> None:
    result = service(home).retrieve_context(task, project=project, budget=budget)
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
    result = propose_from_sources(svc.paths, provider_name=provider, limit=limit)
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
    svc.init_workspace()
    query = "SELECT * FROM memories"
    params: list[str] = []
    if status:
        query += " WHERE status = ?"
        params.append(status)
    query += " ORDER BY created_at DESC"
    with connection(svc.paths.sqlite_path) as conn:
        found = [dict(row) for row in conn.execute(query, params)]
    console.print_json(json.dumps(found))


@memory_app.command("inspect")
def memory_inspect(memory_id: str, home: Optional[Path] = typer.Option(None)) -> None:
    svc = service(home)
    svc.init_workspace()
    with connection(svc.paths.sqlite_path) as conn:
        row = conn.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone()
    if not row:
        console.print(f"Memory not found: {memory_id}")
        raise typer.Exit(1)
    console.print_json(json.dumps(dict(row)))


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


@app.command()
def mcp(home: Optional[Path] = typer.Option(None)) -> None:
    create_mcp(str(home) if home else None).run()


@capture_app.command("agents")
def capture_agents(
    agent: str = typer.Option("all", help="Source to capture: all, codex, claude, opencode, or hyprnote."),
    hyprnote_root: Optional[Path] = typer.Option(None, help="Hyprnote root directory."),
    dry_run: bool = typer.Option(False, "--dry-run"),
    home: Optional[Path] = typer.Option(None),
) -> None:
    svc = service(home)
    svc.init_workspace()
    result = AgentLogCapture(svc.paths, hyprnote_root=hyprnote_root).capture(agent=agent, dry_run=dry_run)
    console.print_json(json.dumps(result.__dict__))


@automation_app.command("run-agent-log-ingest")
def automation_run_agent_log_ingest(
    agent: str = typer.Option("all", help="Source to capture: all, codex, claude, opencode, or hyprnote."),
    hyprnote_root: Optional[Path] = typer.Option(None, help="Hyprnote root directory."),
    home: Optional[Path] = typer.Option(None),
) -> None:
    paths = BrainPaths.from_value(home)
    result = run_agent_log_ingest(paths, agent=agent, hyprnote_root=hyprnote_root)
    console.print_json(json.dumps(as_jsonable(result)))


@automation_app.command("nightly")
def automation_nightly(
    if_due: bool = typer.Option(False, "--if-due", help="Skip when the last successful nightly run is still recent."),
    due_after_hours: int = typer.Option(20, help="Minimum hours between successful nightly runs when --if-due is set."),
    agent: str = typer.Option("all", help="Source to capture: all, codex, claude, opencode, or hyprnote."),
    hyprnote_root: Optional[Path] = typer.Option(None, help="Hyprnote root directory."),
    with_llm_wiki_proposals: bool = typer.Option(False, "--with-llm-wiki-proposals"),
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
        with_llm_wiki_proposals=with_llm_wiki_proposals,
        provider=provider,
    )
    console.print_json(json.dumps(as_jsonable(result)))
    if result.status == "failed":
        raise typer.Exit(1)


@launch_agent_app.command("install")
def launch_agent_install(
    interval: int = typer.Option(600, help="Polling interval in seconds."),
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
    home: Optional[Path] = typer.Option(None),
) -> None:
    paths = BrainPaths.from_value(home)
    uv = shutil.which("uv") or "/opt/homebrew/bin/uv"
    plist = render_launch_agent(Path.cwd(), paths.home, Path(uv), interval=interval)
    console.print_json(json.dumps(plist))


@launch_agent_app.command("install-nightly")
def launch_agent_install_nightly(
    interval: int = typer.Option(3600, help="Wake-check interval in seconds."),
    due_after_hours: int = typer.Option(20, help="Minimum hours between successful nightly runs."),
    with_llm_wiki_proposals: bool = typer.Option(False, "--with-llm-wiki-proposals"),
    provider: Optional[str] = typer.Option(None, help="LLM provider for wiki proposals: codex, openai, anthropic, or ollama."),
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
    provider: Optional[str] = typer.Option(None, help="LLM provider for wiki proposals: codex, openai, anthropic, or ollama."),
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
