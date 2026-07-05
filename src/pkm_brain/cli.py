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
    run_cos_once,
    run_nightly_maintenance,
    run_secondary_tick,
)
from .capture import AgentLogCapture
from .db import connection, rows
from .evals import run_eval
from .cos_policy import promote_policy_for_autonomy
from .llm import cos_provider_status, provider_status
from .memory_proposals import propose_failure_memories_from_sources, propose_memories_from_lineage
from .mcp_server import create_mcp
from .paths import BrainPaths
from .regeneration import backup_runtime_brain, export_human_state, rebuild_facts_from_sources
from .service import BrainService
from .setup_wizard import run_setup_plan
from .sync_acceptance import run_acceptance_report
from .sync_connection import test_connection as run_sync_test_connection
from .sync_setup import add_peer as sync_add_peer_config
from .sync_setup import init_primary as sync_init_primary_config
from .sync_setup import init_secondary as sync_init_secondary_config
from .sync_ssh import first_host_key_with_fingerprint
from .sync_status import format_status_table_rows, local_sync_snapshot
from .sync_transfer import sync_pull as run_sync_pull
from .sync_transfer import sync_push as run_sync_push
from .sync_transfer import sync_run as run_sync_run
from .ui_server import create_ui_server, ensure_ui_token, ui_token_path, validate_ui_bind
from .wiki import lint_wiki
from .wiki_curation_promote import promote_wiki_curation
from .wiki_fact_migration import migrate_existing_wiki_to_facts
from .wiki_facts import curate_all_managed_fact_pages

app = typer.Typer(help="Local personal knowledge management and agent memory tool.")
inspect_app = typer.Typer(help="Inspect documents and chunks.")
index_app = typer.Typer(help="Index health commands.")
db_app = typer.Typer(help="SQLite database maintenance commands.")
wiki_app = typer.Typer(help="Wiki commands.")
memory_app = typer.Typer(help="Typed memory commands.")
context_app = typer.Typer(help="Context lineage and feedback commands.")
llm_app = typer.Typer(help="LLM provider commands.")
embeddings_app = typer.Typer(help="Embedding provider commands.")
cos_app = typer.Typer(help="Chief-of-Staff commands.")
eval_app = typer.Typer(help="Chief-of-Staff eval commands.")
runs_app = typer.Typer(help="Pipeline run commands.")
provenance_app = typer.Typer(help="Provenance validation commands.")
capture_app = typer.Typer(help="Capture external sources into the inbox.")
automation_app = typer.Typer(help="Scheduled automation commands.")
launch_agent_app = typer.Typer(help="macOS LaunchAgent commands.")
sync_app = typer.Typer(help="Primary/Secondary sync commands.")
scheduler_app = typer.Typer(help="Logical scheduler commands.")
app.add_typer(inspect_app, name="inspect")
app.add_typer(index_app, name="index")
app.add_typer(db_app, name="db")
app.add_typer(wiki_app, name="wiki")
app.add_typer(memory_app, name="memory")
app.add_typer(context_app, name="context")
app.add_typer(llm_app, name="llm")
app.add_typer(embeddings_app, name="embeddings")
app.add_typer(cos_app, name="cos")
app.add_typer(eval_app, name="eval")
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


def run_setup_cli(
    *,
    home: Optional[Path],
    role: Optional[str],
    node_id: Optional[str],
    primary_node_id: Optional[str],
    peer_node_id: Optional[str],
    peer_host: Optional[str],
    peer_user: Optional[str],
    peer_brain_home: Optional[Path],
    peer_outbox_path: Optional[Path],
    identity_path: Optional[Path],
    secondary_outbox_path: Optional[Path],
    install_scheduler: bool,
    interval: int,
    dry_run: bool,
    json_output: bool,
    yes: bool,
    force: bool,
) -> None:
    interactive = not yes and not dry_run and not json_output
    paths = resolve_setup_home(home, interactive)
    role = resolve_setup_role(role, interactive)
    if interactive:
        (
            node_id,
            primary_node_id,
            peer_node_id,
            peer_host,
            peer_user,
            peer_brain_home,
            identity_path,
            secondary_outbox_path,
        ) = prompt_setup_fields(
            paths=paths,
            role=role,
            node_id=node_id,
            primary_node_id=primary_node_id,
            peer_node_id=peer_node_id,
            peer_host=peer_host,
            peer_user=peer_user,
            peer_brain_home=peer_brain_home,
            identity_path=identity_path,
            secondary_outbox_path=secondary_outbox_path,
        )
        if role in {"primary", "secondary"} and not install_scheduler:
            install_scheduler = typer.confirm("Install the scheduled sync job after validation?", default=False)
    try:
        result = run_setup_plan(
            paths,
            role=role,
            node_id=node_id,
            primary_node_id=primary_node_id,
            peer_node_id=peer_node_id,
            peer_host=peer_host,
            peer_user=peer_user,
            peer_brain_home=peer_brain_home,
            peer_outbox_path=peer_outbox_path,
            identity_path=identity_path,
            secondary_outbox_path=secondary_outbox_path,
            install_scheduler=install_scheduler,
            interval=interval,
            dry_run=dry_run,
            force=force,
            repo_path=Path.cwd(),
        )
    except (RuntimeError, ValueError) as exc:
        console.print(str(exc))
        raise typer.Exit(1)
    emit_setup_result(result, json_output=json_output)


def resolve_setup_home(home: Optional[Path], interactive: bool) -> BrainPaths:
    if home is not None or not interactive:
        return BrainPaths.from_value(home)
    default_home = BrainPaths.from_value(None).home
    selected = str(typer.prompt("Brain home", default=str(default_home))).strip()
    return BrainPaths.from_value(selected or default_home)


def resolve_setup_role(role: Optional[str], interactive: bool) -> str:
    if role:
        return role
    if not interactive:
        return "single"
    if not typer.confirm("Set up multi-device Primary/Secondary sync now?", default=False):
        return "single"
    return str(typer.prompt("Role for this machine (primary/secondary)")).strip().lower()


def prompt_setup_fields(
    *,
    paths: BrainPaths,
    role: str,
    node_id: Optional[str],
    primary_node_id: Optional[str],
    peer_node_id: Optional[str],
    peer_host: Optional[str],
    peer_user: Optional[str],
    peer_brain_home: Optional[Path],
    identity_path: Optional[Path],
    secondary_outbox_path: Optional[Path],
) -> tuple[Optional[str], Optional[str], Optional[str], Optional[str], Optional[str], Optional[Path], Optional[Path], Optional[Path]]:
    if role == "primary":
        node_id = node_id or str(typer.prompt("Primary node_id")).strip()
        if typer.confirm("Add a Secondary now?", default=False):
            peer_node_id = peer_node_id or str(typer.prompt("Secondary node_id")).strip()
            peer_host = peer_host or str(typer.prompt("SSH host")).strip()
            peer_user = peer_user or str(typer.prompt("SSH user")).strip()
            peer_brain_home = peer_brain_home or Path(str(typer.prompt("Remote Brain home")).strip())
            identity = str(typer.prompt("SSH identity file path (optional)", default="")).strip()
            identity_path = identity_path or (Path(identity) if identity else None)
    elif role == "secondary":
        node_id = node_id or str(typer.prompt("Secondary node_id")).strip()
        primary_node_id = primary_node_id or str(typer.prompt("Expected Primary node_id")).strip()
        default_outbox = paths.outbox / node_id
        outbox = str(typer.prompt("Secondary outbox path", default=str(default_outbox))).strip()
        secondary_outbox_path = secondary_outbox_path or Path(outbox)
    return node_id, primary_node_id, peer_node_id, peer_host, peer_user, peer_brain_home, identity_path, secondary_outbox_path


def emit_setup_result(result: dict[str, object], *, json_output: bool) -> None:
    if json_output:
        console.print_json(json.dumps(result))
        return
    table = Table(title="Brain Setup")
    table.add_column("Field")
    table.add_column("Value")
    table.add_row("home", str(result["brain_home"]))
    table.add_row("role", str(result["role"]))
    table.add_row("node_id", str(result.get("node_id") or ""))
    table.add_row("planned_writes", str(len(result.get("planned_writes", []))))
    table.add_row("validations", ", ".join(result.get("validation_steps", [])))  # type: ignore[arg-type]
    labels = result.get("planned_launch_agent_labels", [])
    table.add_row("launch_agents", ", ".join(labels) if isinstance(labels, list) else "")
    table.add_row("dry_run", str(result.get("dry_run", False)))
    table.add_row("applied", str(result.get("applied", False)))
    if result.get("scheduler_install_blocked"):
        table.add_row("scheduler_blocked", str(result.get("scheduler_block_reason") or "yes"))
    console.print(table)


def ui_startup_lines(host: str, port: int, token: str) -> list[str]:
    return [
        f"Brain UI listening on http://{host}:{port}",
        f"Token: {token}",
    ]


@app.command()
def init(
    home: Optional[Path] = typer.Option(None, help="Brain home directory."),
    wizard: bool = typer.Option(False, "--wizard", help="Run the guided setup flow."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview setup writes when used with --wizard."),
    json_output: bool = typer.Option(False, "--json", help="Print machine-readable setup output when used with --wizard."),
    yes: bool = typer.Option(False, "--yes", help="Run wizard non-interactively."),
) -> None:
    if wizard:
        run_setup_cli(
            home=home,
            role=None,
            node_id=None,
            primary_node_id=None,
            peer_node_id=None,
            peer_host=None,
            peer_user=None,
            peer_brain_home=None,
            peer_outbox_path=None,
            identity_path=None,
            secondary_outbox_path=None,
            install_scheduler=False,
            interval=1800,
            dry_run=dry_run,
            json_output=json_output,
            yes=yes,
            force=False,
        )
        return
    svc = service(home)
    svc.init_workspace()
    console.print(f"Initialized brain workspace at [bold]{svc.paths.home}[/bold]")


@app.command()
def setup(
    role: Optional[str] = typer.Option(None, "--role", help="Setup role: single, primary, or secondary."),
    node_id: Optional[str] = typer.Option(None, "--node-id"),
    primary_node_id: Optional[str] = typer.Option(None, "--primary-node-id"),
    peer_node_id: Optional[str] = typer.Option(None, "--peer-node-id"),
    peer_host: Optional[str] = typer.Option(None, "--peer-host"),
    peer_user: Optional[str] = typer.Option(None, "--peer-user"),
    peer_brain_home: Optional[Path] = typer.Option(None, "--peer-brain-home"),
    peer_outbox_path: Optional[Path] = typer.Option(None, "--peer-outbox-path"),
    identity_path: Optional[Path] = typer.Option(None, "--identity-path"),
    secondary_outbox_path: Optional[Path] = typer.Option(None, "--outbox-path"),
    install_scheduler: bool = typer.Option(False, "--install-scheduler"),
    interval: int = typer.Option(1800, help="Scheduled sync/capture interval in seconds."),
    dry_run: bool = typer.Option(False, "--dry-run"),
    json_output: bool = typer.Option(False, "--json"),
    yes: bool = typer.Option(False, "--yes", help="Run non-interactively; required fields must be provided."),
    force: bool = typer.Option(False, "--force", help="Overwrite an existing sync.yaml."),
    home: Optional[Path] = typer.Option(None, help="Brain home directory."),
) -> None:
    run_setup_cli(
        home=home,
        role=role,
        node_id=node_id,
        primary_node_id=primary_node_id,
        peer_node_id=peer_node_id,
        peer_host=peer_host,
        peer_user=peer_user,
        peer_brain_home=peer_brain_home,
        peer_outbox_path=peer_outbox_path,
        identity_path=identity_path,
        secondary_outbox_path=secondary_outbox_path,
        install_scheduler=install_scheduler,
        interval=interval,
        dry_run=dry_run,
        json_output=json_output,
        yes=yes,
        force=force,
    )


@app.command()
def ui(
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(8765, "--port"),
    allow_lan: bool = typer.Option(False, "--i-understand-this-binds-to-lan"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    home: Optional[Path] = typer.Option(None, help="Brain home directory."),
) -> None:
    paths = BrainPaths.from_value(home)
    try:
        bind = validate_ui_bind(host, allow_lan=allow_lan)
    except ValueError as exc:
        console.print(str(exc))
        raise typer.Exit(1)
    if dry_run:
        if bind["warning"]:
            console.print(bind["warning"])
        console.print_json(json.dumps({**bind, "port": port, "token_path": str(ui_token_path(paths)), "dry_run": True}))
        return
    token = ensure_ui_token(paths)
    if bind["warning"]:
        console.print(bind["warning"])
    for line in ui_startup_lines(host, port, token):
        console.print(line)
    server = create_ui_server(paths, host, port, token=token)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.server_close()


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
    embedding = status["embedding"]
    table.add_row(
        "embedding_provider",
        "ok" if embedding["available"] else "unavailable",
        status["embedding_provider"] if embedding["available"] else f"{status['embedding_provider']} ({embedding['reason']})",
    )
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


@eval_app.command("run")
def eval_run(
    suite: Optional[str] = typer.Option(None, "--suite", help="Eval suite: extraction, routing, topology, conflict, or retrieval."),
    home: Optional[Path] = typer.Option(None),
) -> None:
    svc = service(home)
    svc.init_workspace()
    try:
        result = run_eval(svc.paths, suite=suite)
    except ValueError as exc:
        console.print(str(exc))
        raise typer.Exit(1)
    console.print_json(json.dumps(result))
    if not result["passed"]:
        raise typer.Exit(1)


@context_app.command("feedback")
def context_feedback(
    target_type: str,
    target_id: str,
    useful: bool = typer.Option(False, "--useful", help="Mark the target as useful context."),
    not_useful: bool = typer.Option(False, "--not-useful", help="Mark the target as not useful context."),
    note: Optional[str] = typer.Option(None, "--note"),
    home: Optional[Path] = typer.Option(None),
) -> None:
    if useful == not_useful:
        console.print("Choose exactly one of --useful or --not-useful.")
        raise typer.Exit(1)
    try:
        result = service(home).record_context_feedback(target_type, target_id, useful=useful, note=note)
    except ValueError as exc:
        console.print(str(exc))
        raise typer.Exit(1)
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


@index_app.command("doctor")
def index_doctor(home: Optional[Path] = typer.Option(None)) -> None:
    console.print_json(json.dumps(service(home).index_doctor()))


@index_app.command("optimize")
def index_optimize(
    cleanup_older_than_days: int = typer.Option(1, "--cleanup-older-than-days", help="Delete LanceDB versions older than this many days."),
    home: Optional[Path] = typer.Option(None),
) -> None:
    console.print_json(json.dumps(service(home).optimize_indexes(cleanup_older_than_days=cleanup_older_than_days)))


@index_app.command("rebuild-vectors")
def index_rebuild_vectors(
    delete_backup: bool = typer.Option(False, "--delete-backup", help="Delete the previous LanceDB backup after verification succeeds."),
    missing_only: bool = typer.Option(False, "--missing-only", help="Only write vectors missing from LanceDB."),
    home: Optional[Path] = typer.Option(None),
) -> None:
    result = service(home).rebuild_vector_index(delete_backup=delete_backup, missing_only=missing_only)
    console.print_json(json.dumps(result))
    if result["status"] != "ok":
        raise typer.Exit(1)


@embeddings_app.command("status")
def embeddings_status(home: Optional[Path] = typer.Option(None)) -> None:
    svc = service(home)
    console.print_json(json.dumps(svc.embedding_provider.status(check_available=True)))


@embeddings_app.command("download")
def embeddings_download(home: Optional[Path] = typer.Option(None)) -> None:
    result = service(home).download_embedding_model()
    console.print_json(json.dumps(result))
    if result["status"] == "failed":
        raise typer.Exit(1)


@index_app.command("reset-retrieval")
def index_reset_retrieval(
    yes: bool = typer.Option(False, "--yes", help="Confirm destructive retrieval/index reset."),
    home: Optional[Path] = typer.Option(None),
) -> None:
    if not yes:
        typer.confirm(
            "Delete retrieval events, retrieval lineage, chunks, FTS rows, and LanceDB vectors, then rebuild chunks/vectors from active documents?",
            abort=True,
        )
    result = service(home).reset_retrieval_index()
    console.print_json(json.dumps(result))
    if result["status"] != "ok":
        raise typer.Exit(1)


@db_app.command("reindex-chunks")
def db_reindex_chunks(
    source_type: str = typer.Option("agent_session_log", "--source-type", help="Only reindex documents with this source_type."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Report affected documents and projected chunks without writing."),
    all_documents: bool = typer.Option(False, "--all-documents", help="Reindex all documents of the source type, not only oversized chunks."),
    target_tokens: int = typer.Option(1200, "--target-tokens", help="Maximum tokens per regenerated chunk."),
    overlap_tokens: int = typer.Option(200, "--overlap-tokens", help="Tokens to overlap between split oversized chunks."),
    home: Optional[Path] = typer.Option(None),
) -> None:
    result = service(home).reindex_chunks(
        source_type=source_type,
        dry_run=dry_run,
        all_documents=all_documents,
        target_tokens=target_tokens,
        overlap_tokens=overlap_tokens,
    )
    console.print_json(json.dumps(result))
    if result["status"] == "failed":
        raise typer.Exit(1)


@wiki_app.command("lint")
def wiki_lint(home: Optional[Path] = typer.Option(None)) -> None:
    svc = service(home)
    svc.init_workspace()
    result = lint_wiki(svc.paths)
    console.print_json(json.dumps(result))
    if result["errors"]:
        raise typer.Exit(1)


@wiki_app.command("migrate-to-facts")
def wiki_migrate_to_facts(
    dry_run: bool = typer.Option(True, "--dry-run/--apply", help="Preview by default; pass --apply to write facts."),
    overwrite_existing: bool = typer.Option(False, "--overwrite-existing", help="Allow managed page regeneration to overwrite existing wiki pages."),
    include_references: bool = typer.Option(False, "--include-references", help="Also import reference pages; usually not needed for this one-time migration."),
    home: Optional[Path] = typer.Option(None),
) -> None:
    svc = service(home)
    svc.init_workspace()
    result = migrate_existing_wiki_to_facts(
        svc.paths,
        dry_run=dry_run,
        overwrite_existing=overwrite_existing,
        include_references=include_references,
    )
    console.print_json(json.dumps(result))
    lint_errors = result.get("curation", {}).get("lint_errors", [])
    if lint_errors:
        raise typer.Exit(1)


@wiki_app.command("curate-facts")
def wiki_curate_facts(
    overwrite_existing: bool = typer.Option(False, "--overwrite-existing", help="Allow managed page regeneration to overwrite existing wiki pages."),
    home: Optional[Path] = typer.Option(None),
) -> None:
    svc = service(home)
    svc.init_workspace()
    result = curate_all_managed_fact_pages(
        svc.paths,
        overwrite_existing=overwrite_existing,
    )
    console.print_json(json.dumps(result))
    lint_errors = result.get("curation", {}).get("lint_errors", [])
    final_lint_errors = result.get("lint", {}).get("errors", [])
    projection_errors = [
        error
        for page in result.get("curation", {}).get("pages", [])
        for error in page.get("projection_errors", [])
    ]
    if lint_errors or final_lint_errors or projection_errors:
        raise typer.Exit(1)


@wiki_app.command("promote-curation")
def wiki_promote_curation(
    source_home: Path = typer.Option(..., "--source-home", help="Forked Brain home containing resolved curation state."),
    dry_run: bool = typer.Option(True, "--dry-run/--apply", help="Preview by default; pass --apply to promote curation state."),
    replace_existing: bool = typer.Option(False, "--replace-existing", help="Allow replacing existing target facts/questions/runs."),
    backup: bool = typer.Option(True, "--backup/--no-backup", help="Back up the target DB and wiki before applying."),
    home: Optional[Path] = typer.Option(None),
) -> None:
    target = BrainPaths.from_value(home)
    source = BrainPaths.from_value(source_home)
    BrainService(target).init_workspace()
    result = promote_wiki_curation(
        source,
        target,
        dry_run=dry_run,
        replace_existing=replace_existing,
        backup=backup,
    )
    console.print_json(json.dumps(result))


@llm_app.command("doctor")
def llm_doctor(provider: Optional[str] = typer.Option(None)) -> None:
    console.print_json(json.dumps(provider_status(provider)))


@cos_app.command("providers")
def cos_providers(
    home: Optional[Path] = typer.Option(None, help="Brain home directory."),
    json_output: bool = typer.Option(False, "--json", help="Print machine-readable provider status."),
) -> None:
    result = cos_provider_status(BrainPaths.from_value(home))
    if json_output:
        console.print_json(json.dumps(result))
        return
    table = Table(title="CoS LLM Providers")
    table.add_column("Role")
    table.add_column("Provider")
    table.add_column("Model")
    table.add_column("Effort")
    table.add_column("Ready")
    table.add_column("Source")
    table.add_column("Missing")
    for row in result["roles"]:
        table.add_row(
            str(row["role"]),
            str(row.get("provider") or "unconfigured"),
            str(row.get("model") or ""),
            str(row.get("reasoning_effort") or ""),
            "yes" if row.get("configured") else "no",
            str(row.get("provider_source") or ""),
            ", ".join(row.get("missing") or []),
        )
    console.print(f"Config: {result['config_path']} ({'present' if result['config_exists'] else 'absent'})")
    console.print(table)
    if result["warnings"]:
        console.print("Warnings:")
        for warning in result["warnings"]:
            console.print(f"- {warning}")


@cos_app.command("promote-policy")
def cos_promote_policy(
    reason: str = typer.Option("activate chief-of-staff low/medium autonomy", "--reason"),
    large_topology_fact_threshold: int = typer.Option(8, "--large-topology-fact-threshold"),
    yes: bool = typer.Option(False, "--yes", help="Confirm policy promotion non-interactively."),
    home: Optional[Path] = typer.Option(None, help="Brain home directory."),
) -> None:
    if not yes:
        typer.confirm("Create a new active CoS autonomy policy version?", abort=True)
    paths = BrainPaths.from_value(home)
    BrainService(paths).init_workspace()
    with connection(paths.sqlite_path) as conn:
        version = promote_policy_for_autonomy(
            conn,
            reason=reason,
            large_topology_fact_threshold=large_topology_fact_threshold,
        )
    console.print_json(
        json.dumps(
            {
                "status": "ok",
                "policy_version": version,
                "large_topology_fact_threshold": large_topology_fact_threshold,
            }
        )
    )


@cos_app.command("run")
def cos_run(
    llm_wiki: bool = typer.Option(True, "--llm-wiki/--no-llm-wiki"),
    home: Optional[Path] = typer.Option(None, help="Brain home directory."),
) -> None:
    paths = BrainPaths.from_value(home)
    result = run_cos_once(paths, llm_wiki=llm_wiki)
    console.print_json(json.dumps(result))


@cos_app.command("export-human-state")
def cos_export_human_state(
    output_dir: Optional[Path] = typer.Option(None, "--output-dir", help="Directory for human_state.json."),
    home: Optional[Path] = typer.Option(None, help="Brain home directory."),
) -> None:
    result = export_human_state(BrainPaths.from_value(home), output_dir=output_dir)
    console.print_json(json.dumps(result))


@cos_app.command("backup-runtime")
def cos_backup_runtime(
    output_dir: Optional[Path] = typer.Option(None, "--output-dir", help="Directory for db/wiki backup."),
    home: Optional[Path] = typer.Option(None, help="Brain home directory."),
) -> None:
    result = backup_runtime_brain(BrainPaths.from_value(home), output_dir=output_dir)
    console.print_json(json.dumps(result))


@cos_app.command("rebuild-facts")
def cos_rebuild_facts(
    from_sources: bool = typer.Option(False, "--from-sources", help="Plan a rebuild from active source documents."),
    dry_run: bool = typer.Option(True, "--dry-run/--apply", help="Preview by default; --apply is intentionally blocked for now."),
    source_type: Optional[list[str]] = typer.Option(None, "--source-type", help="Restrict to a source_type; may be repeated."),
    limit: Optional[int] = typer.Option(None, "--limit", min=1, help="Maximum source documents to include."),
    home: Optional[Path] = typer.Option(None, help="Brain home directory."),
) -> None:
    try:
        result = rebuild_facts_from_sources(
            BrainPaths.from_value(home),
            from_sources=from_sources,
            dry_run=dry_run,
            source_types=source_type or [],
            limit=limit,
        )
    except ValueError as exc:
        console.print(str(exc))
        raise typer.Exit(1)
    console.print_json(json.dumps(result))


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


@memory_app.command("propose-from-lineage")
def memory_propose_from_lineage(
    provider: Optional[str] = typer.Option("codex"),
    limit: int = typer.Option(12),
    home: Optional[Path] = typer.Option(None),
) -> None:
    svc = service(home)
    svc.init_workspace()
    result = propose_memories_from_lineage(svc.paths, provider_name=provider, limit=limit)
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
    svc = BrainService(BrainPaths.from_value(home))
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


@sync_app.command("mirror-hash")
def sync_mirror_hash(
    home: Optional[Path] = typer.Option(None),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    result = local_sync_snapshot(BrainPaths.from_value(home))
    if json_output:
        console.print_json(json.dumps(result))
        return
    table = Table(title="Sync Mirror Hash")
    table.add_column("Field")
    table.add_column("Value")
    table.add_row("home", result["brain_home"])
    table.add_row("canonical_manifest_hash", str(result["canonical_manifest_hash"]))
    table.add_row("pending_outbox_count", str(result["pending_outbox_count"]))
    table.add_row("outbox_path", str(result["outbox_path"] or ""))
    console.print(table)


@sync_app.command("rebuild-mirror-index")
def sync_rebuild_mirror_index(
    home: Optional[Path] = typer.Option(None),
) -> None:
    result = service(home).rebuild_mirror_index()
    console.print_json(json.dumps(result))
    if result["errors"]:
        raise typer.Exit(1)


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


@sync_app.command("acceptance")
def sync_acceptance(
    peer_node_id: Optional[str] = typer.Option(None, "--peer", help="Secondary peer node_id. Inferred when exactly one peer is configured."),
    run_sync: bool = typer.Option(False, "--run-sync", help="Execute the real pull/push/remote-ingest sync step."),
    skip_connection: bool = typer.Option(False, "--skip-connection", help="Skip the SSH/rsync connection test."),
    retrieval_phrase: Optional[str] = typer.Option(None, "--retrieval-phrase", help="Unique Secondary session phrase to verify retrieval after sync."),
    home: Optional[Path] = typer.Option(None),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    report = run_acceptance_report(
        BrainPaths.from_value(home),
        peer_node_id=peer_node_id,
        test_connection_now=not skip_connection,
        run_sync_now=run_sync,
        retrieval_phrase=retrieval_phrase,
    )
    if json_output:
        console.print_json(json.dumps(report))
    else:
        table = Table(title="Sync Acceptance")
        table.add_column("Check")
        table.add_column("Status")
        table.add_column("Message")
        for check in report["checks"]:
            table.add_row(check["name"], check["status"], check["message"])
        console.print(f"Home: {report['home']}")
        console.print(f"Peer: {report['peer_node_id'] or 'not selected'}")
        console.print(table)
        console.print(f"Ready: {'yes' if report['ready'] else 'no'}")
        console.print(f"Complete: {'yes' if report['complete'] else 'no'}")
        if not report["complete"]:
            console.print("Run with --run-sync and --retrieval-phrase after the Secondary has produced a unique session.")
    if not report["ready"]:
        raise typer.Exit(1)


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
    with_llm_memory_proposals: bool = typer.Option(False, "--with-llm-memory-proposals"),
    llm_wiki: bool = typer.Option(
        True,
        "--llm-wiki/--no-llm-wiki",
        help="Enable shadow CoS wiki synthesis when a synthesizer provider is configured.",
    ),
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
        with_llm_memory_proposals=with_llm_memory_proposals,
        llm_wiki=llm_wiki,
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
def scheduler_uninstall_sync() -> None:
    from .scheduler.launchd import LaunchdScheduler, SYNC_PRIMARY_LABEL

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
    with_llm_memory_proposals: bool = typer.Option(False, "--with-llm-memory-proposals"),
    llm_wiki: bool = typer.Option(
        True,
        "--llm-wiki/--no-llm-wiki",
        help="Enable shadow CoS wiki synthesis when a synthesizer provider is configured.",
    ),
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
        with_llm_memory_proposals=with_llm_memory_proposals,
        llm_wiki=llm_wiki,
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
    with_llm_memory_proposals: bool = typer.Option(False, "--with-llm-memory-proposals"),
    llm_wiki: bool = typer.Option(
        True,
        "--llm-wiki/--no-llm-wiki",
        help="Enable shadow CoS wiki synthesis when a synthesizer provider is configured.",
    ),
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
        with_llm_memory_proposals=with_llm_memory_proposals,
        llm_wiki=llm_wiki,
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
