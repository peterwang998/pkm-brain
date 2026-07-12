# Product Foundation

**Status:** canonical living feature spec
**Last verified:** 2026-07-11 against public release `0.1.1` code snapshot `b3ba211`
**Owns:** product boundaries, authority hierarchy, persistence model, privacy rules, and cross-feature invariants

## Purpose

PKM Brain is a local-first personal knowledge system and MCP memory server for one operator. It captures private source material, derives searchable documents and source-backed facts, projects those facts into a managed wiki, and returns bounded context to local agents.

The product is an operational tool, not a general document editor or cloud knowledge service. Normal use must remain useful when model providers or vector search are unavailable.

## Authority Hierarchy

The authority order is fixed:

1. Raw source artifacts are durable evidence.
2. Facts are the canonical source-backed knowledge ledger.
3. Entities provide identity across facts; page routes do not define identity.
4. Managed wiki pages are rebuildable projections of active facts.
5. Memories are separately reviewed typed claims.
6. Indexes, retrieval telemetry, summaries, and eval reports are derived artifacts.

No synthesis, wiki prose, model judgment, retrieval frequency, or UI state may become evidence by itself.

## Runtime Boundary

Source code, tests, reusable skills, and documentation live in this repository. Private runtime state lives outside git, normally under `~/brain`:

| Path | Role | Authority |
|---|---|---|
| `inbox/` | connector and manual capture landing area | transient source input |
| `raw/` | normalized source artifacts | durable evidence |
| `wiki/` | human and managed Markdown pages | human content plus derived projection |
| `memory/` | reviewed memory exports | portable export; SQLite remains local canonical state |
| `db/` | SQLite control plane | local canonical metadata and ledgers |
| `indexes/` | FTS/vector artifacts | rebuildable |
| `config/shared/` | portable non-secret configuration | primary-owned when sync is enabled |
| `config/local/` | machine-local provider and behavior configuration | never blindly synced |
| `outbox/` | secondary-node source export | source transport, not canonical state |
| `logs/` and backups | operations and recovery | bounded operational artifacts |

The repository must never contain a live Brain database, indexes, private captures, local provider credentials, or user logs.

## System Flow

```text
capture -> inbox -> ingest -> documents/chunks/raw
  -> FTS and stamped chunk vectors
  -> windowed extraction and deterministic validation
  -> reversible CoS actions and policy gates
  -> facts/entities/open-question residue
  -> managed wiki projection
  -> search/retrieve_context/MCP/UI
```

Ingest remains deterministic and offline-capable. LLM-backed curation runs after ingest and may skip cleanly when its role is not configured.

## Persistent Model

SQLite is the control plane. The current schema applies migrations 1 through 20 idempotently.

| Migration | Durable capability |
|---:|---|
| 1 | origin identity |
| 2 | sync run history |
| 3 | context lineage |
| 4 | retrieval snapshots |
| 5 | sensitivity-column cleanup |
| 6-8 | wiki fact curation, change status, and snapshots |
| 9 | enriched facts |
| 10 | reversible `cos_actions` ledger |
| 11 | versioned `cos_policy` |
| 12 | page contracts |
| 13 | derived wiki syntheses |
| 14 | expanded open-question residue |
| 15 | shared retrieval FTS |
| 16 | fact-grain lineage |
| 17 | CoS stage watermarks |
| 18 | entity identity and fact links |
| 19 | entity mention kind |
| 20 | document source size/mtime statistics |

Major persistent responsibilities:

- `documents` and `chunks` represent ingested sources with provenance.
- `facts` stores atomic claims, evidence quotes/spans, routes, confidence, lifecycle state, and primary entity cache.
- `entities` and `fact_entities` provide resolved named identity and many-to-many fact links.
- `cos_actions` is the universal reversible mutation ledger.
- `cos_policy` records the versioned autonomy policy that decided an action.
- `open_questions` stores uncertainty and human-review residue, not a second action system.
- `page_contracts` constrain page scope and retrieval purpose.
- `wiki_page_syntheses` stores non-canonical derived prose.
- `memories` stores proposed, active, rejected, archived, or otherwise reviewed typed memories.
- retrieval and lineage tables explain what was selected, exposed, or fed to an agent.
- automation and sync tables record local operations; they are not portable source material.

The legacy `wiki_change_*` tables remain compatibility/audit data. Active UI, CLI, MCP, and nightly paths do not create or apply legacy wiki proposal batches.

## Decision Boundary

Deterministic code always owns:

- filesystem and schema mechanics;
- source selection, chunking, watermarks, and provenance reconstruction;
- exact duplicate checks and high-precision structural guards;
- application and inverse capture for every durable mutation;
- policy matching, role gating, and eval-gate enforcement;
- index stamps, sync staging, and transport allowlists.

LLMs may judge:

- which durable claims to propose;
- semantic fact relations when a configured flow enables them;
- page/entity topology candidate disposition;
- optional synthesis and sampled audit.

Judgment and mechanics are separate. An LLM may recommend a transition, but only deterministic code applies it through the ledger. Failure, timeout, or low confidence must produce absence or review residue, never an implicit winner.

## Interfaces

The supported surfaces share the same service primitives:

- `brain` CLI for setup, capture, ingest, search, retrieval, curation, evals, sync, maintenance, and diagnostics;
- MCP tools for search, retrieval, memory proposal/read, feedback, and agent-session recording;
- loopback JSON API used by the native app and optional browser fallback;
- native macOS app for daily review and operations;
- plain Markdown and raw files for direct inspection.

Swift and browser clients never open SQLite or LanceDB directly. UI mutations dispatch to existing action, question, memory, or service functions.

## Privacy And Security

- Default network bind is loopback.
- Daemon API authorization uses a per-boot token stored with owner-only permissions.
- Sync uses Primary-initiated SSH with pinned host keys and explicit path allowlists.
- Model/provider use is explicit per role. Unconfigured roles skip; configured provider failure is visible.
- No analytics or product telemetry leaves the machine.
- Notifications contain status and counts, not private document contents.
- Secrets and private key material are never written into shared config or docs.

Allowed network activity is limited to explicitly configured LLM providers, dependency/runtime provisioning, model downloads, and pinned SSH sync.

## Non-Goals

- Multi-user permissions or realtime collaboration.
- Public hosting or a cloud source of truth.
- Direct multi-writer SQLite or LanceDB replication.
- Treating generated prose as evidence.
- App Store distribution, iOS, or browser-first mobile use.
- A general RDF/OWL reasoning runtime.

## Current Status

The core local pipeline, fact/entity ledger, action policy, managed pages, retrieval, MCP, native daemon/app, and Primary/Secondary sync are implemented. The current schema version is 21.

Explicitly incomplete feature areas are tracked in the owning specs:

- full native Ops and Settings control coverage beyond the implemented scheduler, run, connector, storage, Brain-home, and autonomy surfaces;
- Queue admission prioritization beyond the implemented deterministic risk/group order;
- role mobility, DB snapshot replication, and profiles;
- email capture beyond telemetry prerequisites;
- a complete forgetting/redaction lifecycle.

## Cross-Feature Acceptance

Every feature change must preserve these checks:

1. Evidence provenance remains traceable to raw source text.
2. A failed optional provider does not block deterministic ingest/search.
3. Every autonomous durable mutation has a ledger row and inverse.
4. High-risk truth or topology decisions remain reviewable.
5. Index provider mismatch is loud and never mixes vector spaces.
6. Secondary nodes never become accidental canonical writers.
7. Runtime data stays outside git.

Baseline verification:

```bash
uv run ruff check .
uv run pytest -q
swift test --package-path app
scripts/build-app.sh
```

See [the architecture code guide](../architecture-code-guide.md) for code ownership and [the project audit](../audits/project-audit-2026-07-10.md) for current risks.
