# PKM Brain Project Audit - 2026-07-10

**Status:** frozen point-in-time audit; remediation status is tracked in the implementation plan
**Last verified:** 2026-07-10 against commit `e43e9c1e1287` plus the current working tree
**Implementation posture:** evidence reflects July 10; do not use its counts as current runtime state

## Scope And Method

The audit covered:

- repository structure, package boundaries, and file sizes;
- all active/historical specs and cross-references;
- schema migrations and current feature status;
- Python and Swift static reachability for orphan candidates;
- test inventory and frontend test shape;
- native SwiftUI source plus a live Queue screenshot;
- current runtime, backup, log, index, and SQLite growth;
- previous July 7 findings versus the current implementation.

Static reachability is not proof that a symbol is safe to delete. Public/reflection/entry-point behavior must be tested before removal.

## Baseline

Audited source size:

| Area | Files/lines |
|---|---:|
| Python package | 72 files, 40,861 lines |
| Swift app | 5,282 lines |
| Python tests | 17,438 lines |
| Docs before consolidation | 22 files, 6,895 lines |
| Docs after consolidation and execution planning | 32 files, 3,640 lines (47.2% fewer lines; old paths retained) |

Latest committed baseline is `e43e9c1`, "Implement native wiki entities ask tabs." The worktree also contains the July 10 Queue/relation/popularity/autonomy/model changes and tests. Existing non-document changes were not reverted or rewritten by this audit.

## Findings

### P0.1 - The primary UI displays contradictory Queue totals

**Impact:** high. Review count is a trust and workload signal; contradictory totals make it impossible to know whether work was actually cleared.

**Evidence:**

- live native capture on 2026-07-10 showed sidebar `Queue 260` while the open Queue showed `105 total`;
- `AppState.queueTotal` reads only `digest.queue_counts.total`;
- `QueueView` loads a fresher `QueuePage.total` but does not reconcile it into global app state;
- digest refresh is independent and runs on a 60-second monitor, so errors/staleness can leave the badge authoritative after a successful Queue load.

**Required outcome:** one deduplicated active-Queue definition and one freshness/reconciliation protocol across menu bar, Today, sidebar, notifications, filters, and Queue page.

### P0.2 - A live conflict card is approvable without a candidate fact

**Impact:** high. A reviewer can be asked to make a semantic decision from incomplete evidence.

**Evidence:**

- selected live `question_8` displayed "No fact payload" in the Candidate panel and three Existing panels;
- `question_candidate_fact` only hydrates a candidate from specific option/recommended-action payload shapes;
- `FactPanel` renders a missing candidate as ordinary empty text instead of marking the item non-approvable.

This violates the evidence-first Queue contract even though counterpart hydration and page-split previews were improved.

**Required outcome:** hydrate/migrate candidate-less historical questions into an honest comparison model, or return an invalid/obsolete item that cannot be decided.

### P0.3 - Operational retention is unbounded across multiple roots

**Impact:** high. Obvious runtime/backups consume more than 35 GB and continue growing outside the current prune scope.

Measured 2026-07-10:

| Root | Size | Main contributors |
|---|---:|---|
| `<brain-home>` | 13 GB | 11 GB backups, 886 MB DB, 441 MB indexes, 503 MB logs |
| `<brain-home>-runtime-backups` | 6.6 GB | nine approximately 702-853 MB backups |
| `~/Library/Application Support/PKM Brain` | 15 GB | 14 GB across 16 runtime versions, 816 MB migration data |

Notable subtrees:

- `brain/backups/pkm-brain-repo-local-backups-2026-07-03`: about 10 GB;
- `brain/logs/curation-promotion-backups`: about 440 MB;
- nightly output log: about 51 MB.

`maintenance.py` prunes selected Brain/runtime-backup paths by age. It does not enforce retention on app-managed runtime versions or the large repo backup subtree.

**Required outcome:** inventory-based retention across every product-owned root, keeping current and one verified rollback, with dry-run byte totals and explicit exclusions for user-created backups.

### P0.4 - SQLite remains dominated by operational telemetry

**Impact:** high for disk, lock duration, backup size, and routine query cost.

Largest measured allocations:

| Allocation | Approximate size |
|---|---:|
| `retrieval_events` | 328.7 MB |
| retrieval debug payloads | 235.2 MB |
| retrieval snapshot payloads | 81.1 MB |
| `retrieval_fts_data` | 85.7 MB |
| automation summaries | 62.6 MB |
| `automation_runs` | 64.0 MB |
| `retrieval_fts_content` | 40.9 MB |
| `chunk_fts_content` | 40.4 MB |
| `chunks` | 40.2 MB |

Compaction exists, but stored debug/summaries and retained event detail still make telemetry a major fraction of the 886 MB DB. One concurrent audit count encountered `database is locked`, reinforcing that maintenance/audit reads must avoid competing with live jobs.

**Required outcome:** bounded-at-write payloads, explicit detailed-event horizon, aggregate exposure retention, snapshot/lineage pin rules, and measurable post-VACUUM targets.

### P1.1 - Native frontend status is overstated relative to acceptance

**Impact:** medium-high. Seven destinations exist, but several primary workflows are previews or partial readers.

| View | Implemented strengths | Material gaps |
|---|---|---|
| Shell | native navigation, menu bar, daemon supervision | no Command-K palette/help; global state can be stale |
| Today | pulse, deltas, Queue entry | inherits stale digest count; raw timestamps are not polished |
| Queue | paging, sort, filters, decision/undo, rich topology/policy cards | contradictory badge, candidate-less card, no observed keyboard pass, no relation-aware batch |
| Wiki | search, list/detail, facts/sources | line-based renderer, 20-fact silent cap, no provenance popovers/Confirm/Flag/contract rail/diffs |
| Entities | retrieval/fact/recency sorts, confidence/popularity, facts/co-mentions | merge candidates are rationale text only; no proposal control |
| Ask | verdict and layered packet, local history | suppressed/debug candidates not fully inspectable; weak cross-navigation |
| Ops | scheduler table | no run controls, logs, connectors, ledger/revert, policy/audit, index, sync, maintenance |
| Settings | autonomy, general toggles, daemon restart | editable home lacks apply/validation semantics; connectors/embeddings/agents/sync/diagnostics absent |

The browser fallback exposes some richer operations, but it does not satisfy the native app's own acceptance.

### P1.2 - Frontend tests validate decoding, not rendered workflows

**Impact:** medium-high. Visual/state regressions can pass every current Swift test.

The 14 Swift tests cover fixture decoding, API error messages, handshake URL, daemon restart, and runtime mismatch replacement. There are no rendered-view, accessibility, screenshot, focus, or keyboard workflow tests.

The Python M4 acceptance verifies API/database effects for mixed decisions, which is valuable, but it does not prove the signed SwiftUI app presents complete data or that focus/shortcuts work.

**Required outcome:** seeded UI acceptance for normal/minimum widths and light/dark appearance, plus state tests for count reconciliation, invalid-card disabling, decision rollback/undo, daemon restart, and keyboard navigation.

### P1.3 - Core modules have accumulated too many ownership domains

**Impact:** medium-high. Changes to shared modules carry broad regression risk and make orphan detection/refactoring harder.

| Module | Lines | Responsibilities currently combined |
|---|---:|---|
| `ui_server.py` | 4,921 | HTTP/auth/static, daemon/migration, Queue/query/decision, Wiki, Entities, Settings, Ops |
| `service.py` | 4,404 | workspace/init, ingest, retrieval, memory shaping, telemetry, FTS/index, source parsing |
| `extraction.py` | 3,824 | selection/windowing, prompt/provider, validation, critic/relation, route reclaim, metrics |
| `wiki_facts.py` | 3,522 | fact persistence/resolution, questions, routing, page projection, snapshots |
| `cos_actions.py` | 2,750 | generic ledger plus all operation apply/inverse implementations |
| `cli.py` | 2,224 | command registration plus substantial presentation/orchestration |
| `QueueView.swift` | 1,421 | main view, rows, all card types, batch/undo, keyboard capture, helpers |
| `Models.swift` | 697 | nearly the entire API model surface |

Top-level callable counts are also high: `ui_server.py` 196, `extraction.py` 122, `wiki_facts.py` 114, `service.py` 104, `cos_actions.py` 87.

**Required outcome:** incremental cohesive extraction behind existing public functions, with characterization tests before movement. A broad rewrite would add more risk than it removes.

### P1.4 - Review volume has no durable admission budget

**Impact:** medium. W2a reduced visible work to about 105, but the system can rebuild another large demand queue.

Implemented:

- future-action strict/balanced/lenient policy;
- retrieval-impact ordering;
- deduplicated active topology read surface;
- relation-gated one-time reconciliation.

Not implemented:

- daily review admission budget;
- deferred pool and visible deferred count;
- impact x uncertainty admission;
- aging for safe classes;
- relation-aware batch decisions.

The queue count target is therefore a successful cleanup result, not a maintained service-level bound.

### P1.5 - Legacy state is retained without an explicit retirement contract

**Impact:** medium.

- `wiki_change_batches` and `wiki_change_items` remain in the base schema/migration compatibility tests but have no active UI/CLI/MCP/nightly producers or consumers.
- legacy LaunchAgent commands are intentionally retained for rollback/development and are not orphaned, but their support horizon is undefined.
- active Queue reads hide historical duplicate topology rows; no maintenance command persistently retires them.

**Required outcome:** inventory data/users, define backup/export and compatibility horizon, then retire with a migration and tests rather than deleting opportunistically.

### P1.6 - Critic separation is no longer model-independent

**Impact:** medium. Correlated proposer/critic errors can pass a gate described as independent.

The verified July 10 mapping correctly follows the requested cost-preserving migration:

- extractor, resolver, gardener, synthesizer, and critic use Codex `gpt-5.6-luna` at task-relative effort;
- auditor uses `gpt-5.6-sol` xhigh with Luna fallback.

`brain cos providers` reports warnings because the critic shares the same provider/model as every proposer role. The auditor remains model-separated. Current critic separation is therefore prompt/role/process separation, not provider/model separation.

**Required outcome:** explicitly choose the intended guarantee. Either assign a model-independent critic, or document/measure the correlated-error risk and reserve true independent review for the Sol auditor. Provider doctor and policy language must describe the same guarantee.

### P2.1 - Internal orphan candidates exist

**Impact:** low individually; collectively they increase search surface and false architecture cues.

Textual reachability found only definitions for:

| Candidate | Location |
|---|---|
| `PlaceholderView` | `app/Sources/Views/Placeholder/PlaceholderView.swift` |
| `BrainUIHandler.write_html` | `ui_server.py` |
| `queue_action_count` | `ui_server.py` |
| `get_embedding_provider` | `embeddings.py` |
| `find_entity_by_normalized_name` | `entities.py` |
| `find_entity_by_normalized_alias` | `entities.py` |
| `upsert_primary_fact_entity` | `entities.py` |
| `resolver_precheck_conflict_reason` | `extraction.py` |
| `resolver_precheck_conflict_judgment` | `extraction.py` |

These are deletion candidates, not confirmed dead public API. Remove only after import/search tests and targeted behavior checks.

Confirmed non-orphans:

- `PKMBrainApp` and acceptance entry points are executable targets;
- cron/systemd scheduler adapters are tested explicit stubs;
- wiki curation/migration modules are CLI rollback/migration tools;
- LaunchAgent code is a compatibility path.

### P2.2 - Role mobility and profiles are design-only

**Impact:** medium for disaster recovery and multi-brain use, low for current single-primary operation.

No implementation exists for:

- persistent home-relative path migration;
- consistent DB snapshot replication;
- shared topology record/`primary_epoch`;
- planned/disaster promotion and stale-primary fencing;
- profile registry, concurrent per-profile daemons, or global-source claim routing.

The implemented multi-child star transport should not be described as role mobility.

### P2.3 - Email remains a stale-shaped proposal

**Impact:** low until intentionally resumed.

Only telemetry housekeeping from its Phase 0 exists. There is no Maildir/mbox capture source or email-specific test path. The old plan was too detailed relative to an unapproved connector and risked becoming implied current scope.

The consolidated spec now preserves only the safety constraints. A new implementation plan should begin with a fresh corpus/privacy/retention decision.

### P2.4 - Retrieval extensions are ideas, not current capabilities

**Impact:** low until prioritized.

Fact vectors, query expansion, neighbor expansion, cross-encoder reranking, and semantic gardener/entity candidates are not implemented. They should remain independent eval experiments; bundling them would make attribution and rollback poor.

### P2.5 - Forgetting/redaction lifecycle remains incomplete

**Impact:** medium for long-lived private data.

The system has source cleanup/retention and some redaction rules, but no complete product contract for user-requested forgetting across raw, DB, wiki projections, indexes, snapshots, telemetry, exports, and synced copies.

This was a valid unresolved item in the original V0.1 spec and should become a scoped feature plan rather than historical prose.

## Documentation Audit

Before consolidation, current contracts were spread across 14 active/legacy stream specs plus four archive notes. Completed implementation logs, live verification records, future plans, and current requirements were interleaved.

Confirmed drift included:

- CoS spec stopped at migration 19 while code/schema tests require 20;
- architecture guide contradicted itself about active synthesis;
- README presented LaunchAgents as normal automation after app migration;
- sync spec said real two-machine acceptance and per-peer scheduling were pending after both completed;
- sync runbook expected only migrations 1-2;
- UI docs alternated between six browser destinations and seven native destinations;
- July 7 audit still listed fixed golden-query, re-hash, packet, citation, and memory-audit findings;
- implemented embedding/entity/extraction phases remained hundreds of active lines.

This documentation pass resolves the structure:

- six canonical feature specs under `docs/specs/`;
- one compact code guide;
- one current audit and one separate improvement plan;
- one compact implementation-stream history;
- old paths retained as short compatibility pointers.

## Test And Quality Posture

Strengths:

- broad Python coverage across schema, sync, extraction, actions, policy, relations, UI endpoints, and acceptance;
- deterministic temp-home tests;
- eval suites for semantic quality;
- Swift runtime/supervision tests;
- release and migration acceptance scripts;
- reversible live W2a application with backup/integrity audit.

Residual risks:

- no UI rendering/interaction automation;
- large modules make isolated unit boundaries expensive;
- live storage/retention is not represented by a bounded-growth acceptance test;
- real DB inspection can contend with active jobs;
- several live acceptance claims exist only in historical prose rather than machine-readable release evidence.

## Recommended Order

1. Fix Queue truthfulness: global counts and incomplete cards.
2. Bound app runtimes, backups, logs, and telemetry.
3. Add rendered native acceptance, then finish Wiki/Entities/Ops/Settings.
4. Add review admission budget/deferred pool.
5. Split high-pressure modules incrementally and delete proven orphans.
6. Retire legacy schema/automation under an explicit compatibility plan.
7. Implement recovery/topology prerequisites before role mobility.
8. Re-plan email, forgetting/redaction, and retrieval experiments independently.

The source roadmap is [Project Improvement Plan](../plans/project-improvement-plan.md). The release-sized execution order is [Project Improvement Implementation Plan](../plans/project-implementation-plan.md).
