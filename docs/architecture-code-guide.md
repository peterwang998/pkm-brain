# PKM Brain Architecture Code Guide

**Status:** current code-navigation guide
**Last verified:** 2026-07-14 against the locally release-verified manual Calendar/Gmail shadow implementation

This guide answers where behavior lives. Feature requirements and open work belong in [the specs index](README.md), not here.

## System Map

```text
capture.py / connectors.py / connector_auth.py
  -> service.py ingest
  -> db.py + migrations.py
  -> chunking.py + indexes.py + embeddings.py
  -> extraction.py
  -> cos_actions.py + cos_policy.py
  -> wiki_facts.py + entities.py + gardener.py
  -> service.py retrieval
  -> mcp_server.py / ui_server.py / cli.py

approved operational sources
  -> google_api.py + google_sources.py + google_normalization.py
  -> shadow_trial.py + gmail_operations.py
  -> operational_state.py + operational_shadow.py
  -> operational_db.py + operational_migrations.py + operational_budget.py
  -> db/ops.sqlite
  -> operational_briefing.py + operational_today.py
  -> Today briefing, evidence audit, and local feedback

daemon.py + automation.py schedule the same primitives.
SwiftUI and browser assets call ui_server.py JSON endpoints.
sync_* modules move source files, never live DB/index state.
```

## Authority Map

| Layer | Canonical representation | Main code |
|---|---|---|
| runtime paths | `BrainPaths` rooted at one home | `paths.py` |
| capture state | inbox files and `capture_sources` | `capture.py`, `connectors.py` |
| connector auth | local metadata plus macOS Keychain secrets | `connector_auth.py`, `connector_api.py` |
| source evidence | raw artifacts, `documents`, `chunks` | `service.py`, `chunking.py` |
| lexical/vector index | FTS5 and stamped LanceDB chunks | `indexes.py`, `embeddings.py` |
| facts | `facts` plus exact source spans/quotes | `extraction.py`, `wiki_facts.py` |
| entity identity | `entities` and `fact_entities` | `entities.py` |
| durable mutation | `cos_actions` plus inverse | `cos_actions.py` |
| autonomy | versioned `cos_policy` and eval state | `cos_policy.py`, `evals.py` |
| operational current state | `db/ops.sqlite` items, observations, events, and cursors | `operational_state.py`, `operational_db.py` |
| review residue | `open_questions`, proposed memories, audit flags | `wiki_facts.py`, `memory.py`, `ui_server.py` |
| page projection | contracts, facts, synthesis, snapshots, Markdown | `wiki_facts.py`, `contracts.py` |
| retrieval packet | facts/pages/chunks/memories plus verdict/lineage | `service.py` |
| automation | jobs and `automation_runs` | `daemon.py`, `automation.py` |
| sync | source outbox/staging/mirror and `sync_runs` | `sync_*.py` |

## Package Guide

### Workspace And Persistence

- `paths.py`: all home-relative paths, node identity, lock/token/handshake paths.
- `db.py`: base schema, connection helpers, FTS setup, row helpers.
- `migrations.py`: ordered idempotent migrations 1-21.
- `operational_db.py` and `operational_migrations.py`: the independently versioned `db/ops.sqlite` control plane and bounded lock handling.
- `config.py` and `sync_config.py`: local/shared and role-specific config.

Do not create ad hoc paths or SQLite connections in UI/Swift code.

`brain.sqlite` remains authoritative for knowledge. `ops.sqlite` is authoritative only for current operational state; neither database uses cross-database foreign keys or multi-database write transactions.

### Capture And Ingest

- `connectors.py`: connector registry, manifests, config, health.
- `connector_auth.py`: OAuth provider metadata, state/PKCE/nonce flow, loopback callback, and Keychain storage.
- `connector_api.py`: bounded connector command routing for the JSON API.
- `capture.py`: built-in agent/note capture adapters and snapshot export.
- `service.py`: workspace initialization, source detection, ingest, chunk/index writes, quarantine, status.
- `chunking.py`: deterministic chunk boundaries and evidence text preparation.

Connector output stops at `inbox/`. `BrainService.ingest` is the single entry to raw/document/chunk/index state.

The manually invoked Calendar/Gmail shadow path is not capture output. It uses the separately bound read-only credentials through the operational modules below and never writes `inbox/`, documents, chunks, facts, or wiki pages.

### Embeddings And Indexes

- `embeddings.py`: config resolution, hash/ST providers, unavailable sentinel, query/passsage interface.
- `indexes.py`: LanceDB tables, provider stamp checks, vector rebuild/backfill, index statistics.
- SQLite FTS definitions live in `db.py` and service helpers.

Provider mismatch must disable/refuse the vector channel; never mix spaces or silently substitute hash.

### Facts, Entities, And Pages

- `extraction.py`: document/window selection, prompts, evidence units, validation/retry, watermarks, critic/conflict helpers.
- `fact_relations.py`: typed candidate/counterpart relation classifier.
- `source_evidence.py`: source-date and source-document evidence normalization shared by review projections.
- `routing_coherence.py`: source-document routing priors used without overriding contradictory fact evidence.
- `entities.py`: normalization, resolution, type/mention gates, aliases, link helpers.
- `wiki_facts.py`: fact persistence/lifecycle, open questions, routing, page projection, snapshots, human answers.
- `contracts.py`: page-scope and retrieval-purpose contracts.
- `gardener.py`: deterministic topology candidates, per-candidate LLM disposition, proposal.
- `regeneration.py`: source-driven rebuild workflow and safety/reporting.

Entity identity is not a page path. `fact_entities` is link authority; `facts.entity_id` is a primary-link cache.

### Actions, Policy, Audit, And Evals

- `cos_actions.py`: action type registry, proposal/application, inverse capture, guarded revert, sibling retirement.
- `cos_policy.py`: risk features, ordered policy matching, version activation/demotion.
- `curation_settings.py`: strict/balanced/lenient future-action presets.
- `cos_audit.py`: sampled action audit and demotion.
- `evals.py` and `retrieval_fixtures.py`: extraction/routing/topology/conflict/relations/retrieval gates.
- `fact_review_volume.py`: approval-gated W2 reconciliation reporting/application.
- `review_admission.py`: schema-21 Queue admission budget, grandfathering, deferral, and promotion.
- `policy_reconciliation.py` and `topology_reconciliation.py`: dry-run-first repair commands for legacy review residue.

Semantic judgment may choose a knowledge operation. Only `cos_actions.py` applies durable Knowledge Curation state.

The `cos_*` surface is a legacy physical name for Knowledge Curation. It must not be extended into operational items or external actions.

### Chief-Of-Staff Operational State

- `operational_state.py`: immutable source observations, deterministic source-key reconciliation, operator feedback, current items, event history, and source cursors.
- `operational_db.py`: short-lived operational connections, WAL/foreign-key setup, and bounded lock retry.
- `operational_migrations.py`: independently versioned `ops_*` schema migrations.
- `operations_policy.py` and `shadow_setup.py`: strict owner-only policy, approved 7/30-day privacy controls, and exact account/scope binding.
- `google_api.py`, `google_sources.py`, `google_normalization.py`, and `google_routes.py`: bounded read-only Google transport, resumable Calendar/Gmail change reads, deterministic normalization, and safe provider routes.
- `google_cache.py`: owner-only disposable raw and revision-addressed normalized evidence with separate retention lanes.
- `gmail_llm.py` and `gmail_operations.py`: restricted Gmail detector provider boundary, bounded structured detection, deterministic evidence/lifecycle validation, and source-local handled assessment.
- `operational_budget.py`: durable local-day API/call/token reservations.
- `operational_shadow.py`: shadow runs, decisions, handled assessments, and missing reports.
- `shadow_trial.py` and `shadow_controller.py`: one background manual Calendar/Gmail pass, atomic resume/cursor behavior, coverage, retention, and polling state.
- `operational_briefing.py`, `operational_today.py`, and `today_presentation.py`: deterministic briefing projection, ignored/suppressed audit, evidence navigation, and local feedback contracts.
- `paths.py`: `BrainPaths.ops_sqlite_path` resolves the operational database inside the same Brain home.

The provider-read-only/local-write manual shadow slice is implemented and locally release-verified. The first owner-started private evaluation, cross-source episode linking, scheduling, production trust, and guarded execution remain gated by [Chief-of-Staff Operations](specs/chief-of-staff-operations.md).

### Retrieval And Memory

- `service.py`: search, layer selection, reranking, verdict/calibration, context packet, lineage, telemetry compaction.
- `memory.py` and `memory_proposals.py`: typed memory lifecycle and model/deterministic proposals.
- `mcp_server.py`: small stable agent-facing tool surface.

Retrieval exposure is telemetry. It can order review work but cannot alter truth confidence.

### Automation And Operations

- `automation.py`: capture/nightly stage orchestration and summaries.
- `daemon.py`: loopback API process, serial scheduler registry, token/lock/handshake, parent monitor.
- `maintenance.py`: bounded storage inventory and dry-run-first backup cleanup; user-created brain backups are never auto-pruned.
- `launch_agents.py` and scheduler adapters: legacy/development automation.
- `ui_server.py`: auth HTTP handler, JSON endpoints, Queue projection/dispatch, browser static serving.
- `cli.py`: command registration and presentation over lower-level functions.

The app daemon is normal production automation. LaunchAgent commands are compatibility paths.

### Sync

- `sync_config.py`: role/peer config and validation.
- `sync_ssh.py`: SSH command and host-key handling.
- `sync_rsync.py`: pure rsync command construction and allowlists.
- `sync_transfer.py`: staged pull, validation, ingest, push, remote rebuild.
- `sync_service.py`: high-level status/run/acceptance operations.

The primary initiates transfer. Source artifacts cross machines; databases and indexes do not.

## Frontends

### Native

- `app/Sources/App`: scenes, navigation, app state, menu bar, notifications.
- `app/Sources/Kit`: API models/client, daemon supervisor, runtime provisioner, process-aware runtime retention.
- `app/Sources/Views`: Today, Queue, Wiki, Entities, Ask, Ops, Settings.
- `app/Sources/Views/Today/TodayView.swift` and `TodayEvidenceSheet.swift`: manual Shadow start/progress, briefing/audit sections, local feedback, missing reports, and retained evidence inspection.
- `app/Sources/Acceptance`: headless app/runtime acceptance harness.
- `app/UITests`: rendered navigation and Queue keyboard acceptance coverage.

Wiki Markdown uses Apple's `swift-markdown` package and bounded explicit fact disclosure. Queue, Wiki, Entities, Ask, Ops, and Settings share typed API models and deep-link state. `QueueView.swift` and `Models.swift` remain decomposition candidates documented in the audit.

### Browser

- `src/pkm_brain/ui_static/index.html`: shell.
- `app.js` and view modules: route/view state.
- `api.js`: authenticated API client.
- `app.css` and `tokens.css`: layout and visual tokens.

The browser and native clients consume the same endpoints and mutation primitives.

## Public Surfaces

- `cli.py` registers `brain`.
- `mcp_server.py` registers agent tools.
- `ui_server.py` owns local HTTP routes.
- `daemon.py` owns normal app-supervised process lifetime.
- `app/Package.swift` and `project.yml` own Swift package/Xcode builds.
- `scripts/build-app.sh` owns release app assembly.
- `scripts/install-app.sh` owns verified `/Applications` staging, rollback, activation, and login-item installation.
- `scripts/ui-acceptance.sh` owns isolated rendered macOS UI acceptance.

## Current Pressure Points

Largest Python modules in the audited working tree:

| File | Lines | Mixed responsibilities |
|---|---:|---|
| `ui_server.py` | 6,032 | HTTP/auth, all endpoints, Queue, Wiki/Entities/Ops, migration, static serving |
| `service.py` | 4,920 | init/ingest, retrieval, telemetry, FTS/index helpers, parsing |
| `extraction.py` | 4,402 | selection, prompt/provider calls, validation, relation/critic helpers, metrics |
| `wiki_facts.py` | 3,540 | fact lifecycle, questions, routing, page projection, snapshots |
| `cos_actions.py` | 3,353 | generic ledger plus every action implementation/inverse |

Largest Swift view is `QueueView.swift` at 1,952 lines. These are not automatically bugs, but they increase ownership ambiguity and make focused tests/refactors harder. `tests/test_architecture_boundaries.py` now enforces these exact counts as no-growth ceilings: behavior must move into focused modules before a guarded file can grow.

See [the current audit](audits/project-audit-2026-07-10.md) and [implementation plan](plans/project-implementation-plan.md) before moving code.

## How To Change The System

When adding a capture source:

1. implement a connector that writes inbox artifacts and state;
2. reuse ingest/source detection;
3. add source-weight, redaction, and eval fixtures;
4. do not write facts/indexes from the connector.

When adding a durable curation operation:

1. define deterministic candidate/evidence;
2. add action type plus inverse and guarded revert;
3. classify risk and policy;
4. add eval/audit coverage;
5. expose the existing action through UI, not a new write path.

When changing retrieval:

1. add or update fixtures first;
2. measure verdict, source hit, precision, calibration, noise, and negative controls;
3. keep provider/index degradation explicit;
4. verify context packet size and telemetry growth.

When changing UI:

1. update the shared endpoint contract;
2. keep browser/native writes on the same primitive;
3. test complete data and error states;
4. verify rendered light/dark and minimum/normal window sizes;
5. reconcile global counts after mutations.

When adding a repair or reconciliation:

1. use a shared scan/report/approve/apply shape;
2. make dry-run the default and record provenance on application;
3. keep ordinary future work behind `review_admission.py` rather than bypassing Queue limits;
4. archive or consolidate mission-specific repair code when its migration is complete.

When adding an operational source or transition:

1. normalize an immutable observation with replay-stable provider authority;
2. prefer deterministic provider identity before semantic matching;
3. apply the item change and append its event in one `ops.sqlite` transaction;
4. expose missing coverage and ambiguity instead of manufacturing an all-clear;
5. keep source mutation behind the separate guarded-execution boundary.

## Specs

- [Product Foundation](specs/product-foundation.md)
- [Capture And Knowledge](specs/capture-and-knowledge.md)
- [Retrieval And Memory](specs/retrieval-and-memory.md)
- [Curation And Review](specs/curation-and-review.md)
- [Chief-Of-Staff Operations](specs/chief-of-staff-operations.md)
- [App And Operations](specs/app-and-operations.md)
- [Sync And Topology](specs/sync-and-topology.md)
