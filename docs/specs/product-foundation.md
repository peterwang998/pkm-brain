# Product Foundation

**Status:** canonical living feature spec; the manual read-only Chief-of-Staff trial is locally release-verified and testing-ready, while its first owner-authorized live run and promotion remain pending
**Last verified:** 2026-07-14 with Ruff green, 736 Python tests, 26 Swift tests, and a signed local app bundle built; no live connector result is claimed
**Owns:** product boundaries, authority hierarchy, persistence model, privacy rules, and cross-feature invariants

## Purpose

PKM Brain is one local-first product for one operator whose mission is to function proactively as a personal Chief of Staff. The existing knowledge system is the evidence, memory, and policy foundation for that mission; it is not a separate product or a replaceable implementation detail.

One app, daemon, Brain home, and service boundary contain two bounded contexts with different authority and lifecycles:

1. **Knowledge Curation** captures private source material, derives searchable documents and source-backed facts, projects those facts into a managed wiki, maintains reviewed memories, and returns bounded knowledge context to local agents.
2. **Operational Chief of Staff** detects time-sensitive signals, reconciles commitments and attention items over time, produces freshness-aware briefings, records operator feedback, and may later plan guarded external actions.

The contexts share source evidence and identity references, but neither may silently write the other's canonical state. Facts use truth maintenance and durable provenance. Operational items use state transitions, due/expiry semantics, and reconciliation. Folding both contexts into Brain does not collapse those lifecycles into one schema or model pipeline.

The product is an operational tool, not a general document editor or cloud knowledge service. Normal use must remain useful when model providers or vector search are unavailable.

## Authority Hierarchy

Knowledge authority remains fixed:

1. Raw source artifacts are durable evidence.
2. Facts are the canonical source-backed knowledge ledger.
3. Entities provide identity across facts; page routes do not define identity.
4. Managed wiki pages are rebuildable projections of active facts.
5. Memories are separately reviewed typed claims.
6. Indexes, retrieval telemetry, summaries, and eval reports are derived artifacts.

No synthesis, wiki prose, model judgment, retrieval frequency, or UI state may become evidence by itself.

Operational authority is separate:

1. Source observations and stable evidence references explain why an operational item exists or changed.
2. The current item row in `ops.sqlite` is canonical for the universal lifecycle state `active|resolved|dismissed|cancelled|expired`; waiting is an item kind, not another state.
3. Append-only item events preserve detection, reconciliation, correction, transition, and feedback history.
4. Briefings, urgency scores, summaries, and notification text are derived views, not operational authority.
5. External-action plans, approvals, receipts, and compensations form an execution audit trail; they are not knowledge evidence unless separately captured through the normal source pipeline.

An operational observation does not become a durable fact merely because it was detected. When operational material warrants durable knowledge, it re-enters the existing Knowledge Curation proposal, policy, critic, and ledger path.

## Runtime Boundary

Source code, tests, reusable skills, and documentation live in this repository. Private runtime state lives outside git, normally under `~/brain`:

| Path | Role | Authority |
|---|---|---|
| `inbox/` | connector and manual capture landing area | transient source input |
| `raw/` | normalized source artifacts | durable evidence |
| `wiki/` | human and managed Markdown pages | human content plus derived projection |
| `memory/` | reviewed memory exports | portable export; SQLite remains local canonical state |
| `db/brain.sqlite` | Knowledge Curation control plane | local canonical knowledge metadata and ledgers |
| `db/ops.sqlite` | Operational Chief-of-Staff control plane | local canonical operational state and execution audit |
| `indexes/` | FTS/vector artifacts | rebuildable |
| `config/shared/` | portable non-secret configuration | primary-owned when sync is enabled |
| `config/local/` | machine-local provider and behavior configuration | never blindly synced |
| `outbox/` | secondary-node source export | source transport, not canonical state |
| `logs/` and backups | operations and recovery | bounded operational artifacts |

The repository must never contain a live Brain database, operational database, indexes, private captures, local provider credentials, or user logs.

`ops.sqlite` is a separate physical database under the same Brain home and daemon. It references evidence and knowledge through stable IDs resolved by the service layer; it does not duplicate source bodies or require a cross-database transaction with `brain.sqlite`. Cross-context work is idempotent and replay-safe so failure in one context cannot leave an untracked mutation in the other.

## System Flow

```text
                                      +-> Knowledge Curation
capture -> inbox -> ingest -> evidence|   -> extraction/validation
                                      |   -> Knowledge Curation ledger/policy
                                      |      (physical cos_actions/cos_policy today)
                                      |   -> facts/entities/wiki/memories
                                      |   -> knowledge search/retrieve_context
                                      |
                                      +-> Operational Chief of Staff
                                          -> source observations
                                          -> deterministic + bounded semantic reconciliation
                                          -> current items + append-only item events
                                          -> freshness-aware Today briefing
                                          -> guarded action plan/approval/commit/receipt
```

Ingest remains deterministic and offline-capable. LLM-backed curation runs after ingest and may skip cleanly when its role is not configured.

These are two evidence flows over shared source identity. The operational flow must not inherit the Knowledge Curation extractor/critic/resolver loop by default. It performs one bounded structured detection pass where needed, prefers provider IDs and deterministic reconciliation, and represents unresolved ambiguity as low-confidence operational state. Knowledge Curation continues to use its existing evidence, policy, critic, and reversible-mutation controls unchanged.

## Persistent Model

`brain.sqlite` remains the Knowledge Curation control plane. The current schema applies migrations 1 through 21 idempotently. Introducing `ops.sqlite` is additive and must not change the meaning, migration history, or behavior of existing knowledge tables.

| Migration | Durable capability |
|---:|---|
| 1 | origin identity |
| 2 | sync run history |
| 3 | context lineage |
| 4 | retrieval snapshots |
| 5 | sensitivity-column cleanup |
| 6-8 | wiki fact curation, change status, and snapshots |
| 9 | enriched facts |
| 10 | reversible Knowledge Curation ledger, physically named `cos_actions` |
| 11 | versioned Knowledge Curation policy, physically named `cos_policy` |
| 12 | page contracts |
| 13 | derived wiki syntheses |
| 14 | expanded open-question residue |
| 15 | shared retrieval FTS |
| 16 | fact-grain lineage |
| 17 | Knowledge Curation stage watermarks, physically named `cos_stage_watermarks` |
| 18 | entity identity and fact links |
| 19 | entity mention kind |
| 20 | document source size/mtime statistics |
| 21 | review admission metadata and state |

Major persistent responsibilities:

- `documents` and `chunks` represent ingested sources with provenance.
- `facts` stores atomic claims, evidence quotes/spans, routes, confidence, lifecycle state, and primary entity cache.
- `entities` and `fact_entities` provide resolved named identity and many-to-many fact links.
- `cos_actions` is the universal reversible **Knowledge Curation** mutation ledger. The physical name is retained for compatibility until an all-at-once migration; it is not the Operational Chief-of-Staff item or execution ledger.
- `cos_policy` records the versioned Knowledge Curation autonomy policy that decided a knowledge mutation. The same compatibility rule applies to its physical name.
- `open_questions` stores uncertainty and human-review residue, not a second action system.
- `page_contracts` constrain page scope and retrieval purpose.
- `wiki_page_syntheses` stores non-canonical derived prose.
- `memories` stores proposed, active, rejected, archived, or otherwise reviewed typed memories.
- retrieval and lineage tables explain what was selected, exposed, or fed to an agent.
- automation and sync tables record local operations; they are not portable source material.

The legacy `wiki_change_*` tables remain compatibility/audit data. Active UI, CLI, MCP, and nightly paths do not create or apply legacy wiki proposal batches.

`ops.sqlite` owns operational observations, current item state, append-only item events, briefing metadata, explicit feedback/corrections, and guarded-execution plan/approval/receipt records. It does not own raw evidence, facts, entities, wiki pages, reviewed memories, or Knowledge Curation policy. Operational current state and its event history are authoritative and therefore must be backed up; generated briefing prose is rebuildable.

## Decision Boundary

Deterministic code always owns:

- filesystem and schema mechanics;
- source selection, chunking, watermarks, and provenance reconstruction;
- exact duplicate checks and high-precision structural guards;
- application and inverse capture for every durable Knowledge Curation mutation;
- policy matching, role gating, and eval-gate enforcement;
- index stamps, sync staging, and transport allowlists;
- operational source identity, state-machine legality, due/expiry calculations, deterministic reconciliation keys, and staleness classification;
- external-action capability allowlists, exact payload hashes, precondition checks, declared reversibility class, approval binding, commit, verification, and receipt recording.

LLMs may judge:

- which durable claims to propose;
- semantic fact relations when a configured flow enables them;
- page/entity topology candidate disposition;
- optional synthesis and sampled audit;
- operational signal classification, bounded ambiguous-item linkage, and briefing wording or prioritization.

Judgment and mechanics are separate. An LLM may recommend a transition, but only deterministic code applies it through the owning Knowledge Curation ledger or operational transition service. Failure, timeout, or low confidence must produce absence, labeled low-confidence operational state, or review residue, never an implicit winner.

External execution is a stricter boundary than internal state mutation. Each capability declares `read_only`, `reversible`, `compensable`, or `irreversible`, and follows:

```text
plan -> bind approval to exact payload and preconditions -> commit -> verify -> audit receipt
```

Draft-only behavior precedes any commit capability. A compensation is recorded as a new action and never described as perfect rollback; sent mail and other irreversible effects remain explicitly irreversible. No model, briefing, connector, or operational item may bypass this boundary.

## Interfaces

The supported surfaces share the same service primitives:

- `brain` CLI for setup, capture, ingest, search, retrieval, curation, evals, sync, maintenance, and diagnostics;
- MCP tools for search, retrieval, memory proposal/read, feedback, and agent-session recording;
- loopback JSON API used by the native app and optional browser fallback;
- native macOS app for daily review and operations;
- plain Markdown and raw files for direct inspection.

Swift and browser clients never open either SQLite database or LanceDB directly. UI mutations dispatch to the owning Knowledge Curation, operational, question, memory, or service function.

## Privacy And Security

- Default network bind is loopback.
- Daemon API authorization uses a per-boot token stored with owner-only permissions.
- Sync uses Primary-initiated SSH with pinned host keys and explicit path allowlists.
- Model/provider use is explicit per role. Unconfigured roles skip; configured provider failure is visible.
- No analytics or product telemetry leaves the machine.
- Notifications contain status and counts, not private document contents.
- Secrets and private key material are never written into shared config or docs.
- Operational action scopes use the narrowest approved provider capability and keep secrets in machine-local credential storage.
- The approved private Calendar/Gmail trial keeps separate exact-scope read-only grants, retains owner-only raw resumable payloads for 7 days and normalized revision evidence for 30 days, never fetches attachments, strips quoted Gmail history before normalized retention, and enables no external provider writes.
- Raw operational detector prompts and responses are not retained by default; the live Gmail detector is restricted, tool-less, explicitly budgeted, and cannot bypass deterministic evidence and lifecycle validation.

Allowed network activity is limited to explicitly configured LLM, connector, and guarded-action providers; dependency/runtime provisioning; model downloads; and pinned SSH sync.

## Non-Goals

- Multi-user permissions or realtime collaboration.
- Public hosting or a cloud source of truth.
- Direct multi-writer SQLite or LanceDB replication.
- Treating generated prose as evidence.
- App Store distribution, iOS, or browser-first mobile use.
- A general RDF/OWL reasoning runtime.
- A second Chief-of-Staff app, daemon, connector-auth stack, or scheduler.
- Treating operational item state as a fact, or treating a fact as current operational state without an operational observation and transition.
- Letting a secondary node reconcile, approve, or execute canonical operational work.

## Current Status

The core local Knowledge Curation pipeline, fact/entity ledger, action policy, managed pages, retrieval, MCP, native daemon/app, and Primary/Secondary sync are implemented. The current schema version is 21. Existing `cos_*` code and tables implement Knowledge Curation despite their historical names.

The Operational Chief-of-Staff context now has an isolated, independently migrated `ops.sqlite` kernel, a daemon-owned fail-closed mutation service, coordinated database-pair recovery, read-only Calendar/Gmail source integration, deterministic reconciliation, a coverage-aware Today briefing, local feedback, and offline replay evaluation. The daemon initializes and serves the operational store, but does not schedule the shadow pass: the owner must authorize two exact-scope Google grants and select **Today > Run Shadow** for every live pass. Operational writes remain local to `ops.sqlite`; Calendar/Gmail knowledge ingestion and all external-action authority remain disabled. Restores publish only into a new quarantined home and cannot become a writer until a future explicit topology activation workflow.

The 2026-07-14 local release gate is complete: Ruff is green, 736 Python tests and 26 Swift tests pass, and the signed local app bundle builds. That makes the manual workflow testing-ready; observed signed-app UI acceptance remains a separate final gate. Neither establishes live connector success, private-source quality, daily briefing trust, or promotion. The first owner-authorized live trial and the owning spec's empirical gates remain pending.

The item lifecycle, source-specific detection, reconciliation, briefing, evaluation, and execution protocol are specified in [Chief-of-Staff Operations](chief-of-staff-operations.md). This foundation owns the boundary between that context and existing Knowledge Curation.

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
3. Every autonomous durable Knowledge Curation mutation has a ledger row and inverse; operational corrections append a new event rather than rewriting history.
4. High-risk truth or topology decisions remain reviewable.
5. Index provider mismatch is loud and never mixes vector spaces.
6. Secondary nodes never become accidental canonical writers.
7. Runtime data stays outside git.
8. Operational state is written only through `ops.sqlite` on the active primary and never changes a knowledge row directly.
9. Knowledge and operational retrieval preserve their separate trust, lifecycle, and freshness semantics.
10. Once operational state is initialized, a coordinated recovery set cannot mix unmatched `brain.sqlite` and `ops.sqlite` generations.
11. External effects require a capability declaration, payload-bound approval, precondition recheck, and durable receipt.

Baseline verification:

```bash
uv run ruff check .
uv run pytest -q
swift test --package-path app
scripts/build-app.sh
```

See [the architecture code guide](../architecture-code-guide.md) for code ownership and [the project audit](../audits/project-audit-2026-07-10.md) for current risks.
