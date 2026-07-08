# PKM Brain

[![CI](https://github.com/peterwang998/pkm-brain/actions/workflows/ci.yml/badge.svg)](https://github.com/peterwang998/pkm-brain/actions/workflows/ci.yml)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-3776AB)](pyproject.toml)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

PKM Brain is a local-first personal knowledge system and MCP memory server for coding agents. It ingests messy private sources, turns durable claims into a source-backed fact ledger, renders managed wiki pages from those facts, and returns bounded context to agents.

Core rule:

```text
Raw sources are evidence.
Facts are the canonical knowledge ledger.
Managed wiki pages are rebuildable projections.
Memories are reviewed typed claims.
Indexes and eval reports are derived artifacts.
```

Private runtime data lives in `~/brain` by default. The git repo should contain code, tests, docs, and reusable skills only.

## Quickstart

```bash
uv sync
uv run brain init --home ~/brain
uv run brain ingest --home ~/brain
uv run brain search "sqlite metadata" --debug --home ~/brain
uv run brain retrieve-context --task "what does Brain know about this project?" --home ~/brain
uv run brain mcp --home ~/brain
```

Optional local sentence-transformer embeddings:

```bash
uv sync --extra embeddings
uv run brain embeddings download --home ~/brain
uv run brain index rebuild-vectors --home ~/brain
```

## Runtime Layout

```text
~/brain/
  inbox/       captured or manually dropped source files
  raw/         copied source artifacts used as evidence
  wiki/        human-readable Markdown pages
  memory/      optional exported memory Markdown
  db/          SQLite control plane
  indexes/     LanceDB vectors and related indexes
  logs/        LaunchAgent and runtime logs
  config/      local config, sync config, role providers
  evals/       local eval inputs, including golden_queries.yaml
  outbox/      secondary-node export artifacts
```

## Architecture

The main flow is:

```text
capture / inbox
  -> ingest
  -> documents + chunks + raw artifacts
  -> FTS / vector indexes
  -> LLM extraction into candidate facts
  -> CoS actions + policy + critic/audit gates
  -> facts + entities + open-question residue
  -> managed wiki pages
  -> search / retrieve_context / MCP
```

Key layers:

- `documents` and `chunks` store source-derived text with provenance.
- SQLite FTS5 provides BM25 lexical search; LanceDB stores chunk vectors.
- `facts` stores atomic source-backed claims with quotes, spans, routes, status, and confidence fields.
- `entities` and `fact_entities` link facts to named people, companies, products, projects, and other named referents.
- `cos_actions` is the reversible mutation ledger for fact, page, entity, and topology changes.
- `cos_policy` decides autonomy level; clean low/medium-risk actions can apply, while conflicts and ambiguous topology become residue.
- `open_questions` is the human review queue for conflicts, unrouted facts, anomalies, policy escalations, and other residue.
- Managed wiki pages are rendered from active facts. Optional synthesis is derived prose and never becomes evidence.
- `brain eval run` gates extraction, topology, conflict, routing, and retrieval behavior.

The detailed code-derived guide is [docs/architecture-code-guide.md](docs/architecture-code-guide.md). The docs index is [docs/README.md](docs/README.md).

## Chief Of Staff

The Chief-of-Staff layer is the autonomous curation system. It extracts facts from eligible source windows, validates quotes/spans, routes facts to canonical pages, resolves safe duplicates, flags conflicts, applies reversible actions, samples audits, and lets the gardener propose topology cleanup.

Useful commands:

```bash
uv run brain cos providers --home ~/brain
uv run brain cos run --home ~/brain
uv run brain cos queue-summary --home ~/brain
uv run brain eval run --home ~/brain
uv run brain wiki curate-facts --home ~/brain
```

Provider roles are configured in `~/brain/config/local/cos_llm.yaml` or with `PKM_BRAIN_LLM_<ROLE>_*` environment variables. Roles are `extractor`, `resolver`, `gardener`, `synthesizer`, `critic`, and `auditor`.

## Browser UI

```bash
uv run brain ui --home ~/brain
```

The UI is a local stdlib HTTP server with token auth. It exposes the unified review queue, wiki/entity browsing, retrieval inspection, operations status, and review actions. By default it binds to loopback only.

## MCP Tools

The MCP server exposes a compact agent-facing surface:

- `search_knowledge(query, limit)`
- `retrieve_context(task, project)`
- `get_project_context(project)`
- `get_memories(scope, memory_type, status)`
- `propose_memory(memory_type, scope, content, sources, confidence)`
- `record_context_feedback(target_type, target_id, useful, note)`
- `write_agent_session(summary, files_touched, commands_run, outcome, unresolved_issues)`

After the macOS app migration, register agents against the app-managed shim:

```bash
~/Library/Application\ Support/PKM\ Brain/bin/brain-mcp --home ~/brain
```

For repo development, run the direct DB server with:

```bash
uv run brain mcp --home ~/brain
```

## Automation

macOS LaunchAgents support frequent capture and nightly maintenance:

```bash
uv run brain launch-agent install --home ~/brain
uv run brain launch-agent install-nightly --home ~/brain
uv run brain launch-agent status --home ~/brain
uv run brain doctor --home ~/brain
```

Nightly maintenance captures/ingests sources, runs CoS extraction/gardener/synthesis/audit stages according to provider configuration, compacts telemetry, optimizes indexes, checks provenance, lints wiki pages, and audits memories. Secondary sync nodes skip mutation-capable CoS stages by default.

Runtime cleanup is dry-run-first:

```bash
uv run brain maintenance prune --home ~/brain
```

Pass `--commit` only after reading the reported actions.

## Sync

Primary/Secondary sync is file/outbox based, not multi-writer SQLite replication. The primary owns the canonical DB; secondaries can export captured artifacts for staged ingest.

```bash
uv run brain sync init-primary --node-id primary --home ~/brain
uv run brain sync init-secondary --node-id secondary --home ~/brain
uv run brain sync add-peer secondary --host mac-mini.local --path ~/brain --home ~/brain
uv run brain sync status --home ~/brain
uv run brain sync acceptance --home ~/brain
```

See [docs/primary-secondary-brain-sync-spec.md](docs/primary-secondary-brain-sync-spec.md).

## Development

```bash
uv run ruff check .
uv run pytest -q
uv run brain eval run --home ~/brain
```

The package has one optional dependency group:

```bash
uv sync --extra embeddings
```

Current specs and task lists live under [docs/README.md](docs/README.md). Historical plans are in `docs/archive/`.
