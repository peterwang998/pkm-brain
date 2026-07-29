# PKM Brain

[![CI](https://github.com/peterwang998/pkm-brain/actions/workflows/ci.yml/badge.svg)](https://github.com/peterwang998/pkm-brain/actions/workflows/ci.yml)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-3776AB)](pyproject.toml)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

PKM Brain is a local-first personal knowledge system and proactive personal Chief of Staff. Its working knowledge layer ingests messy private sources, turns durable claims into a source-backed fact ledger, renders managed wiki pages, and returns bounded context to agents. Its first operational layer is implemented as a manual read-only Calendar/Gmail shadow trial that maintains current commitments, Calendar state, attention, and briefings without conflating them with durable facts.

Core rule:

```text
Raw sources are evidence.
Facts are the canonical knowledge ledger.
Operational items are the canonical current-work ledger.
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
  logs/        automation and runtime logs
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
  -> knowledge-curation actions + policy + critic/audit gates
  -> facts + entities + open-question residue
  -> managed wiki pages
  -> search / retrieve_context / MCP

approved Calendar/Gmail evidence
  -> operational observations + deterministic reconciliation
  -> current items + append-only transitions
  -> freshness-aware Today briefing
```

Key layers:

- `documents` and `chunks` store source-derived text with provenance.
- SQLite FTS5 provides BM25 lexical search; LanceDB stores chunk vectors.
- `facts` stores atomic source-backed claims with quotes, spans, routes, status,
  confidence, knowledge/source clocks, optional predicate validity, and optional
  event occurrence time.
- `entities` and `fact_entities` link facts to named people, companies,
  products, projects, and other named referents. Events are typed entities;
  their occurrence anchors remain source-backed facts.
- `cos_actions` is the legacy-named reversible Knowledge Curation ledger for fact, page, entity, and topology changes.
- `cos_policy` decides knowledge-mutation autonomy; clean low/medium-risk actions can apply, while conflicts and ambiguous topology become residue.
- `open_questions` is the human review queue for conflicts, unrouted facts, anomalies, policy escalations, and other residue.
- Managed wiki pages are rendered from active facts. Optional synthesis is derived prose and never becomes evidence.
- `ops.sqlite` is the separate operational store for source-backed observations, current items, transition history, and briefing feedback.
- `brain eval run` gates extraction, topology, conflict, routing, and retrieval behavior.

The detailed code-derived guide is [docs/architecture-code-guide.md](docs/architecture-code-guide.md). The docs index is [docs/README.md](docs/README.md).

## Chief Of Staff

Chief of Staff is the product mission, not another app. It uses Brain's evidence and compiled knowledge to proactively surface what needs attention, maintain temporal operational state, and eventually prepare or execute tightly guarded external actions.

The existing autonomous subsystem is Knowledge Curation: it extracts facts from eligible source windows, validates quotes/spans, routes facts to canonical pages, resolves safe duplicates, flags conflicts, applies reversible actions, samples audits, and lets the gardener propose topology cleanup. Its current `cos_*` names remain compatibility identifiers until an atomic rename; they must not be used for operational items or external side effects.

The initial operational rollout is read-only and manual. After the owner separately authorizes the owned-primary Calendar grant and Gmail read-only grant, **Today > Run Shadow** performs one bounded pass, shows source coverage, surfaces operational candidates, explains ignored/suppressed material, opens retained local evidence, and accepts local corrections or missing-item reports. There is no provider write capability. The encrypted Gmail mirror and local Knowledge projection update incrementally in separate bounded jobs when explicitly enabled; mailbox writes remain impossible, and external-LLM extraction from private Gmail is disabled by default behind a separate source-type gate.

The implementation has passed local code, unit, integration, security-boundary, and signed-build verification and is ready for the first owner-started private trial. It has not yet been promoted as trusted daily guidance. See [Chief-of-Staff Operations](docs/specs/chief-of-staff-operations.md) and the [Live Shadow Trial Runbook](docs/runbooks/chief-of-staff-shadow-trial.md).

Useful commands:

```bash
uv run brain cos providers --home ~/brain
uv run brain cos run --home ~/brain
uv run brain cos queue-summary --home ~/brain
uv run brain eval run --home ~/brain
uv run brain wiki curate-facts --home ~/brain
```

Provider roles are configured in `~/brain/config/local/cos_llm.yaml` or with `PKM_BRAIN_LLM_<ROLE>_*` environment variables. Roles are `extractor`, `resolver`, `gardener`, `synthesizer`, `critic`, and `auditor`.

## macOS App

The native app is the normal daily and background surface on macOS. It supervises
the Python daemon, schedules capture/nightly/sync jobs, exposes Today, Queue,
Wiki, Entities, Ask, Ops, and Settings, and keeps the CLI/MCP service available
through app-managed shims.

Install the full app from source on macOS:

```bash
brew install uv xcodegen
uv sync --dev
scripts/build-app.sh
scripts/install-app.sh --activate
```

The installed app lives at `/Applications/PKM Brain.app`. It adopts `~/brain`
in place; private data does not move into the bundle. Re-running the build and
installer upgrades in place while retaining one previous app bundle for local
rollback.

Public source builds are currently Apple Silicon (`arm64`) only and are ad-hoc
signed on the machine that builds them. A Developer ID-signed, notarized, and
universal downloadable release is a separate distribution step; cloning and
building from source is the supported public installation path for this
release.

## Browser Fallback

```bash
uv run brain ui --home ~/brain
```

The browser is an off-by-default fallback over the same loopback JSON API and
mutation primitives as the native app. It remains useful for platform
portability and diagnostics. By default it binds to loopback only.

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

Normal macOS automation runs inside the app-supervised daemon. Jobs include
capture, nightly maintenance, Secondary export, and one independent sync job per
configured peer. Ops and the menu bar expose due/running/paused state and
preserve no-op reasons.

For headless development:

```bash
uv run brain daemon --home ~/brain
uv run brain doctor --home ~/brain
```

Nightly maintenance captures/ingests sources, runs Knowledge Curation extraction/gardener/synthesis/audit stages according to provider configuration, compacts telemetry, optimizes indexes, checks provenance, lints wiki pages, and audits memories. Secondary sync nodes skip mutation-capable curation stages by default. Operational polling/reconciliation is a separate primary-only job family.

Legacy `brain launch-agent` commands remain for migration rollback and
development compatibility; they are not the normal app-managed path.

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

See [the Sync And Topology spec](docs/specs/sync-and-topology.md) and
[acceptance runbook](docs/runbooks/sync-acceptance.md).

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
