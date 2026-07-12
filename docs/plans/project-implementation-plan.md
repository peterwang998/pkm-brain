# Project Improvement Implementation Plan

**Status:** execution in progress; R1-R4 complete, R5/R6 partially complete in release `0.1.1`
**Last verified:** 2026-07-11 against public release `0.1.1` code snapshot `b3ba211`
**Inputs:** [Project Audit](../audits/project-audit-2026-07-10.md) and the six [canonical feature specs](../README.md)

## Objective

Execute the improvement roadmap in releases that:

- restore trust in human-review data first;
- prevent disk and Queue growth from recurring;
- add frontend acceptance before expanding the native UI;
- finish the native product before broad internal refactoring;
- preserve reversible, eval-gated, local-first behavior;
- defer topology expansion, email, and retrieval research until their prerequisites are real.

This plan selects implementation order and interfaces. It does not authorize live data deletion, policy promotion, fact rebuilds, topology role changes, or email ingestion.

## Execution Snapshot

Implemented in `0.1.1`:

- Queue truth/card safety, source dates, conflict/routing/audit repairs, and active/blocked/deferred state;
- migration 21 durable admission with a 100 ordinary-item ceiling and 25 ordinary admissions per day;
- bounded retrieval/automation telemetry and managed storage inventory;
- immutable runtime identity, shared model-cache use, process-aware retention, `/Applications` installer, and version `0.1.1`;
- macOS CI, isolated XCUITest screenshots for all seven destinations, and Queue number/Return acceptance;
- Markdown Wiki, complete fact disclosure, provenance, Confirm/Flag, contracts/snapshots;
- structured Entity merge proposal and Ask debug/deep-link workflows;
- Scheduler/Runs/Connectors/Storage Ops sections and rollback-safe Brain-home switching;
- no-growth ratchets for the six largest modules plus removal of three confirmed orphans.

Verified rollout results, 2026-07-11:

- the installed `/Applications/PKM Brain.app` serves schema 21 from runtime `0.1.1` with the configured sentence-transformer index active;
- process-aware activation reduced app runtime storage from 36 directories/34.71 GB to 6 directories/5.83 GB while retaining every live-process runtime;
- the live Queue reports 46 actionable, 0 blocked, and 0 deferred items after migration-21 grandfathering;
- Python, Swift, and isolated seven-destination XCUITest suites pass, and the release artifact contains only the `0.1.1` wheel.

Still open in the current release map:

- critic assurance decision, relation-aware batch review, and safe aging;
- remaining Ops policy/ledger/index/sync/log parity and Settings model/agent/sync forms;
- Command-K/help and broader light/dark/minimum-width accessibility fixtures;
- full decomposition, forgetting/redaction, legacy retirement, mobility/profiles, and gated R9 work.

## Sequencing Decisions

The source improvement plan is directionally correct, with four execution changes:

1. **Rendered UI acceptance moves directly behind Queue trust.** New native surfaces should not accumulate before the signed app can test state, focus, accessibility, and screenshots.
2. **Review admission moves ahead of broad native polish.** W2a reduced the Queue once; a daily budget/deferred pool must stop it from growing back.
3. **Critic independence blocks automatic aging, not basic deferral.** The system may defer/rank work while the Luna/Luna critic tradeoff is evaluated, but it may not age deferred semantic work into autonomy first.
4. **Module splitting follows behavior stabilization.** Queue, retention, telemetry, and native contracts need characterization before code is moved.

## Priority Model

Priority is ordered by:

1. correctness/data safety;
2. direct user impact and frequency;
3. ability to prevent recurring cost/work;
4. dependency value for later features;
5. implementation/recovery risk.

Relative sizes:

- **S:** one cohesive module/surface;
- **M:** several files within one subsystem;
- **L:** Python/API/Swift or schema-spanning;
- **XL:** multiple independently released prerequisites.

## Prioritized Build And Hit List

| Rank | Build | Priority | Size | Definition of hit |
|---:|---|---|---|---|
| 1 | Queue truth and card safety | P0 | L | all count surfaces agree; no incomplete card is approvable; blocked repair work does not compete with the default backlog; historical conflicts support subset selection |
| 2 | Fact source dates in review UI | P0 | S | every fact card shows its observed source date or an explicit provenance-date fallback |
| 3 | Native rendered acceptance harness | P0 | L | seven destinations, keyboard Queue, light/dark, accessibility run in CI |
| 4 | Unified retention manager | P0 | M | every product-owned GB is classified; current+rollback retained; dry-run/commit proven |
| 5 | Telemetry storage budget | P0 | L | payloads bounded, popularity/lineage preserved, copied DB materially shrinks |
| 6 | Review admission budget and deferred pool | P1 | L | routine new work stays within daily/active limits; deferred work remains visible |
| 7 | Critic assurance decision | P1 | M | policy and doctor state the measured Luna/Luna or model-separated guarantee honestly |
| 8 | Relation-aware batch review and safe aging | P1 | L | homogeneous batches are reversible; only approved safe classes may age |
| 9 | Native Wiki completion | P1 | L | real Markdown, complete facts, provenance popovers, Confirm/Flag, contracts/diffs |
| 10 | Native Entities and Ask completion | P1 | M | policy-gated merge proposal and full retrieval inspection/navigation |
| 11 | Native Ops and Settings completion | P1 | XL | operational parity, safe home switch, connector/model/agent/sync controls |
| 12 | Shell navigation and polish | P1 | M | Command-K/help, readable time, stable restart state, long-content layout |
| 13 | Characterized module decomposition and orphan removal | P2 | XL | ownership boundaries shrink without behavior/latency regression |
| 14 | Forgetting and redaction lifecycle | P2 | XL | one explicit, testable deletion contract spans source, DB, indexes, backups, sync |
| 15 | Legacy schema and LaunchAgent retirement | P2 | L | archived data remains inspectable; compatibility horizon is met |
| 16 | Recovery, role mobility, and profiles | P2 | XL | snapshot+epoch fencing precede handover, disaster promotion, and profiles |
| 17 | Email evidence connector | P3 | XL | only after the owner approves a revised privacy/volume spec |
| 18 | Retrieval quality experiments | P3 | L each | one isolated eval-gated experiment at a time with attribution and rollback |

Ranks 1-8 are the immediate product program. Ranks 9-12 complete the native product. Ranks 13-16 strengthen the platform. Ranks 17-18 remain explicitly gated.

## Release Map

| Release | Outcome | Work packages | Depends on |
|---|---|---|---|
| R0 | frozen execution baseline | BASE-1, BASE-2 | current worktree integrated |
| R1 | trustworthy review surface | TRUST-1, TRUST-2, TRUST-3 | R0 |
| R2 | testable UI and bounded storage | TEST-1, GROWTH-1, GROWTH-2 | R1 for UI; R0 for storage |
| R3 | sustainable human review | REVIEW-1, QUALITY-1, REVIEW-2, REVIEW-3 | R1-R2 |
| R4 | complete native knowledge workflows | WIKI-1, ENTITY-1, ASK-1 | TEST-1, R1 |
| R5 | complete native operations | OPS-1, SETTINGS-1, SHELL-1 | TEST-1, GROWTH-1/2 |
| R6 | maintainable internals | ARCH-1 through ARCH-4 | R1-R5 contracts stable |
| R7 | privacy and compatibility cleanup | PRIVACY-1, LEGACY-1/2 | GROWTH-1, ARCH-1 |
| R8 | recoverable/mobile topology | TOPO-1 through TOPO-5 | R7 data contracts |
| R9 | new inputs and retrieval research | EMAIL-1, RETRIEVAL-1..N | explicit approval/eval gates |

R2's UI and storage lanes may run in parallel in separate branches. The default single-agent sequence is TEST-1, GROWTH-1, then GROWTH-2.

## R0 - Establish The Execution Baseline

### BASE-1 - Integrate The Current Worktree

The current tree already contains Queue integrity, W2a, popularity, confidence, autonomy, model-role, app, and documentation changes. Do not build new work on an ambiguous uncommitted baseline.

Steps:

1. Review and group current code/docs/tests into logical commits.
2. Run full Python/Swift/release gates.
3. Record the resulting baseline commit in this plan and canonical specs.
4. Preserve unrelated user changes; do not flatten them into cleanup commits.

Exit:

- cleanly reviewable baseline commit(s);
- 420+ Python and 14+ Swift tests remain green;
- signed app runtime reports schema 21, version `0.1.1`, and its immutable runtime ID;
- no product behavior is inferred only from an uncommitted tree.

### BASE-2 - Capture Baseline Evidence

Record machine-readable/non-private baselines:

- Queue actionable/group totals and API latency;
- app screenshots for all seven destinations;
- disk inventory by managed root;
- DB table/payload allocation;
- retrieval popularity sample;
- eval reports;
- runtime/backup versions;
- full test/build durations.

Do not run live SQLite inspection while a rebuild, compaction, or parallel critic update is active. Pause/wait for the job, then inspect.

Artifact: `artifacts/baselines/<date>/summary.json` or another gitignored report path referenced by the release record.

## R1 - Trustworthy Review Surface

Implementation checkpoint, 2026-07-10:

- implemented: PID/home-bound freshness summary in digest, Queue, decision, and undo responses;
- implemented: native/browser reconciliation, active/actionable/blocked counts, complete-card validation, and direct POST gating;
- implemented: observed/document source dates, chunk-to-document resolution, blocked-card presentation, and explicit entity-merge direction;
- implemented: default actionable-only paging with a separate Needs Repair view, plus nonempty subset selection for symmetric historical fact groups;
- implemented: extraction anomalies use explicit Confirm Quality Issue / False Positive dispositions, and sampled-bad audits show the applied fact, auditor rationale, and explicit revert/keep effects;
- implemented: one topology-bias control over inverse merge/page-split candidate admission for future gardener runs, with a balanced midpoint and no current-Queue rewrite;
- implemented: pairwise contradiction plus resolver confirmation before conflict admission, with dry-run/apply reconciliation for historical conflict cards;
- implemented: bounded same-document route coherence for uncertain extraction, reclaim, and Inbox suggestions without overriding explicit high-confidence outliers;
- implemented: retrieval eval and sync-acceptance telemetry isolation plus an idempotent legacy eval-lineage purge;
- implemented: focus-independent native number shortcuts for historical fact toggles and a real Return-key binding for Keep Selected;
- implemented: focus-independent native number shortcuts for Inbox route candidates, with text-entry focus protection and Return submission for custom page paths;
- implemented: immediate native Queue transition loading plus request identities so late group/state/sort responses cannot display or mutate stale cards;
- implemented: sampled audit can demote only an attributed policy rule whose own threshold is breached; unscoped findings cannot demote, and unrelated active rules retain their autonomy/critic/sample settings;
- implemented: dry-run-first Policy escalation reconciliation that re-decides current L0-L2 actions through the critic/action ledger and retains current L3 work;
- implemented: dry-run-first topology reconciliation that removes stale admission and duplicate rows, reruns gardener judgment, arbitrates merge before split, and policy-routes the surviving canonical actions;
- implemented: Settings hides internal policy versions and reports a persisted human-readable last-saved timestamp;
- implemented: state-aware sampled-audit admission shared by Queue counts, rows, and decisions; topology cards now show merge direction, current page/contract state, affected counts, and representative facts, while drifted fact findings use targeted ledgered correction and obsolete drifted topology findings are excluded;
- implemented: Hyprnote capture restores true multi-channel chronology with stable speaker labels, explicitly groups legacy synthetic-clock tracks, suppresses metadata-only placeholders, and carries speaker identity through evidence units;
- implemented: fact critic review distinguishes unsupported statements from incomplete evidence packets, performs one bounded citation union plus fresh review, falls back from malformed entity disambiguation, and isolates parallel action failures;
- verified: the incremental approved W2a pass resolved 19 later candidates with zero failures and annotated 51 survivors, leaving the live deduplicated Queue at 230 actionable and 0 blocked;
- verified: the pairwise-v2 apply released all 82 active fact-conflict cards, including 6 resolver-confirmed coexistence cases, and found no missing candidate payloads; the audit-demoted L3 policy retained the released candidates as Policy reviews rather than allowing a policy bypass;
- verified: restored More Autonomy policy v12 reclassified all 167 active Policy questions as L2; normal critic/ledger reconciliation applied 104 facts plus one synthesis, rejected 62 candidates, left zero Policy questions, and passed SQLite integrity with an exact +104 fact delta;
- hardened after live contention: reconciliation parallelizes critic calls but serializes ledger writes with reusable verdicts and bounded whole-operation SQLite retry;
- verified: topology reconciliation reduced 205 open merge/split rows / 104 candidates to 11 L3 candidates, applied 2 small merges, critic-rejected 3 actions, retired 101 duplicate rows, and passed SQLite integrity with zero reconciliation failures;
- verified: audit reconciliation restored one refused fact-audit revert, auto-resolved its generic drift residue, excluded two stale page-merge findings, and left 12 actionable audit findings in a 79-item live Queue; both live SQLite and the pre-repair backup passed integrity checks;
- verified: targeted reprocessing closed all nine legacy extraction anomalies; 50/72 rebuilt critic-reviewed facts applied, 22 unsupported or over-broad facts were rejected, three genuine review tasks were created and only one remained after concurrent manual review, and the final restarted live Queue measured 58 actionable with zero blocked items and zero extraction anomalies;
- verified: 465 Python tests, 15 Swift tests, full Ruff, signed build, healthy embedded daemon restart, and live dark-mode Queue screenshot inspection;
- remaining in R1: maintenance audit/report, historical comparison migration, automated overlap/state tests, and the full signed keyboard/light/dark/minimum-width acceptance matrix.

### TRUST-1 - One Queue Summary Contract

**Outcome:** menu bar, Today, sidebar, notifications, filters, and Queue use the same active-item definition and freshness ordering.

Proposed API model:

```json
{
  "as_of": "server timestamp",
  "server_pid": 12345,
  "home": "/path/to/brain",
  "actionable_total": 105,
  "blocked_total": 1,
  "deferred_total": 0,
  "by_kind": {"conflicts": 19, "topology": 58},
  "raw": {}
}
```

Implementation:

1. Extract one `build_queue_summary(conn)` path from `queue_counts`/descriptor rules.
2. Make `/api/queue` and `/api/digest` return that exact model.
3. Include the post-mutation summary in decision and undo responses.
4. Add `QueueSummary` to Swift models and `AppState`.
5. Track request sequence/server `as_of` so a late digest cannot replace a fresher Queue response.
6. Reconcile summary after load, decision, undo, daemon restart, and manual refresh.
7. Use `actionable_total` for the primary badge; expose blocked/deferred separately.

Primary touchpoints:

- `src/pkm_brain/ui_server.py`;
- `app/Sources/Kit/Models.swift` and `APIClient.swift`;
- `app/Sources/App/AppState.swift`, `MainWindowView.swift`, `MenuBarContent.swift`;
- `TodayView.swift` and `QueueView.swift`;
- browser Queue/Today state.

PR split:

1. Python summary model/invariants.
2. Swift/browser state reconciliation.
3. signed-app acceptance and live read-only check.

Tests:

- summary total equals retrievable descriptors for every filter;
- overlapping digest/Queue responses cannot regress freshness;
- decision and undo update every surface immediately;
- daemon replacement clears old-home/old-process summary.

Exit hit: the 260-versus-105 state cannot be reproduced.

### TRUST-2 - Card Completeness And Approval Gate

**Outcome:** incomplete historical/current items remain visible for repair but cannot mutate knowledge.

Implementation:

1. Add a server-side `validate_queue_card(item)` contract by kind.
2. Return `approvable`, `blocking_code`, and human `blocking_reason`.
3. Hydrate candidate/counterpart/action/source rows before validation.
4. Return `source_date` on every fact, preferring `observed_at` and then the newest captured/created/ingested source-document timestamp; keep the raw timestamp inspectable.
5. Resolve both direct document IDs and chunk-backed provenance so source title and date survive into the review card.
6. Convert unambiguous historical candidate-less questions into the correct comparison representation.
7. Classify unrecoverable active items as blocked; exclude them from the default actionable backlog while retaining them under `state=blocked` for repair. Do not silently delete them.
8. Rebuild and validate the item inside `ui_queue_decision` immediately before mutation.
9. Disable decision/batch controls in Swift/browser and expose blocked items through a distinct Needs Repair mode; route richer repair operations to Ops reporting when available.
10. Add `brain cos queue-audit --json` (or equivalent service report) for invalid active items.

Minimum required fields:

| Kind | Required before approval |
|---|---|
| fact conflict | candidate or explicit `alternatives` historical-comparison model, counterpart evidence, source dates, relation/orientation |
| policy | linked action, effect, evidence/source date for fact mutations, policy reason |
| page split | source page, non-empty resulting children/facts |
| entity merge | active source/destination entities and direction |
| unrouted | candidate fact and valid semantic route choices; no reference/index/internal-provider pages |
| memory/audit | content/finding and exact outcome |

Primary touchpoints:

- `queue_item_for_question`, `question_candidate_fact`, card builders, and `ui_queue_decision`;
- `QueueItem` Swift model and all card/action controls;
- browser card/action rendering;
- CLI/service audit report.

Exit:

- live `question_8` is hydrated or blocked;
- native and browser fact cards show the same source date and provenance fallback;
- zero unexplained approvable items in the audit;
- direct POST cannot bypass the UI gate;
- blocked counts are honest and separate from actionable totals.

### TRUST-3 - R1 Signed-App Acceptance

Run a seeded mixed Queue and a live read-only pass:

- counts agree across all surfaces;
- every kind renders complete evidence or disabled repair state;
- 20-item keyboard flow;
- decision, rollback-on-error, undo, and daemon restart;
- normal and minimum window widths;
- light and dark appearance.

Do not mutate the live Queue until seeded acceptance is green and the live read-only payload is complete.

## R2 - Testable UI And Bounded Storage

### TEST-1 - Native Rendered Acceptance Harness

**Outcome:** future frontend completion has an automated release gate.

Implementation:

1. Add a deterministic seeded temp-home fixture covering every view/card/status, long labels, zero/error/loading states, and blocked/deferred states.
2. Add a macOS UI test target through `project.yml`/XcodeGen; retain Swift package tests for decoding/supervision.
3. Launch the app against the seeded home/runtime with stable test arguments.
4. Test navigation, focus, keyboard commands, decisions, undo, restart, and Settings persistence.
5. Capture named light/dark screenshots at minimum and normal widths.
6. Assert accessibility labels/traits for confidence bands, icon controls, disabled actions, and focused cards.
7. Add the UI target to macOS CI without private runtime data.

Primary touchpoints:

- `project.yml`, `app/Tests` or a new `app/UITests`;
- `app/Sources/Acceptance`;
- build/CI scripts and deterministic fixtures.

Exit:

- seven-destination suite runs locally and in CI;
- R1 count/completeness regressions fail it;
- screenshots are nonblank and free of clipping/overlap;
- private live Brain content is absent from fixtures/baselines.

### GROWTH-1 - Unified Retention Manager

**Outcome:** product-managed runtime/backups/logs have one inventory and safe policy.

Release `0.1.1` checkpoint: the urgent app-runtime slice is implemented and live-verified. Storage inventory classifies Brain, runtime-backup, app-runtime, migration, SQLite, index, log, and user-backup roots. App activation uses fail-closed process-aware retention and reclaimed 28.9 GB while preserving current, rollback, and all live-process runtimes. The broader shared commit/manifest engine for logs, backups, and migration copies remains open; user-created Brain backups stay manual-only.

Implementation:

1. Introduce a typed retention inventory record:
   `path, root, kind, owner, size, created_at, version, current, rollback, pinned, eligible, reason`.
2. Extend `maintenance.py` to cover:
   - `<brain>/backups`;
   - sibling `brain-runtime-backups`;
   - app `runtime/<version>`;
   - app migration/runtime backups;
   - product-owned log rotations and curation backups.
3. Mark unknown/unrecognized and user-managed paths ineligible by default.
4. Add count/age limits per kind; keep active runtime plus at least one smoke-tested rollback.
5. Make app runtime activation call the same inventory/policy after successful smoke/flip.
6. Generate a JSON recovery manifest for committed actions.
7. Expose inventory/dry-run in CLI first; Ops UI waits for R5.

Command posture:

```bash
brain maintenance inventory --home ~/brain --json
brain maintenance prune --home ~/brain                 # dry run
brain maintenance prune --home ~/brain --commit        # explicit only
```

Tests:

- mixed product/user/unknown roots;
- current symlink and rollback survival;
- interrupted commit/re-run idempotence;
- clean-machine provisioning, migration rollback, MCP proxy;
- second dry run is empty after an approved commit.

Live rollout:

1. build inventory only;
2. the owner reviews classifications and byte totals;
3. copied-root deletion rehearsal;
4. explicit live approval;
5. commit and rollback smoke.

The 10 GB repo backup remains ineligible unless ownership is proven or the owner explicitly opts it in.

### GROWTH-2 - Telemetry Storage Budget

**Outcome:** detailed telemetry has a horizon; aggregates/popularity remain durable and cheap.

Correctness checkpoint, 2026-07-11: retrieval eval and sync-acceptance probes no longer write production telemetry. A targeted dry-run-first repair removes only exact golden-query lineage from legacy events and preserves those retrieval rows under an eval caller label. This fixes popularity contamination but does not complete the retention horizon, aggregate design, or storage compaction work below.

Live verification removed 39,115 lineage rows attached to 2,475 exact golden-query retrieval events, affecting 948 fact popularity values. The events remain under caller `eval:retrieval_legacy`, zero retain lineage, and a second dry run is empty. Northwind career/offer examples fell from 26-78 apparent retrievals to 0 because all of their counted exposures came from eval runs.

Design before migration:

1. Inventory which retrieval/snapshot/feedback/audit rows pin detail.
2. Define bounded write formats for non-debug and debug retrieval events plus automation summaries.
3. Define a detailed-event horizon (initial proposal: 30 days, locally configurable).
4. Preserve distinct fact/entity exposure aggregates needed by popularity before deleting detail.
5. Decide whether aggregates live in existing lineage rows or a dedicated daily aggregate table.

Implementation:

- cap/truncate structured fields at write with explicit `truncated` metadata;
- compact in bounded transactions with busy timeout/backoff;
- never compact while rebuild/critic workers are writing;
- retain pinned snapshot/feedback/audit lineage;
- optimize FTS and report optional VACUUM/rebuild savings;
- add synthetic one-month growth coverage.

Primary touchpoints:

- `BrainService.compact_retrieval_events` and retrieval-event writes;
- automation run summary writes;
- lineage/popularity readers;
- `maintenance.py`/CLI and tests;
- migration only if a durable aggregate table is chosen.

Copied-live-DB acceptance:

- popularity counts/order unchanged;
- pinned lineage resolves;
- retrieval/relations/eval gates unchanged;
- no new lock failures;
- detailed payload bytes drop by an agreed measured target;
- projected monthly growth remains under a recorded budget.

Live compaction requires scheduler pause, verified backup, dry run, explicit approval, integrity check, and post-run API/eval smoke.

## R3 - Sustainable Human Review

### REVIEW-1 - Admission Budget And Deferred Pool

**Outcome:** routine work cannot recreate a 500+ actionable Queue.

Do not create a second approval system. Add scheduling metadata only.

Proposed table:

```text
review_admissions(
  source_type, source_id, state,
  first_seen_at, admitted_at, deferred_at,
  priority_score, priority_version,
  policy_version, reason, updated_at
)
```

States:

- `actionable`: admitted to the human Queue;
- `deferred`: valid active work waiting for admission;
- `blocked`: invalid/obsolete data requiring repair;
- terminal state remains owned by the source action/question/memory, not this table.

Policy:

- `review_budget_per_day` limits routine new admissions;
- `max_active_review_items` bounds routine active backlog;
- contradictions, failed audits, anomalies, and unsafe topology are urgent and never auto-resolved, but are reported separately if they exceed the routine cap;
- ranking combines retrieval impact, uncertainty, risk, age, and blast radius with a versioned formula;
- deferred items are visible/filterable and countable in Today/menu/Queue/Ops.

Implementation:

1. Add migration/repository in a new cohesive module, not `ui_server.py`.
2. Reconcile active source candidates into admission metadata idempotently.
3. Run admission after nightly candidate generation and after human decisions free capacity.
4. Add Queue filters/API summary fields and native/browser presentation.
5. Add strict/balanced/lenient policy fields without reclassifying existing rows on setting change.

Exit:

- synthetic high-volume week respects daily and active routine limits;
- deferred items are queryable and lossless;
- urgent items remain visible;
- changing autonomy affects future admissions only;
- every admission decision has score/version/reason.

### QUALITY-1 - Decide Critic Independence

**Outcome:** the word "critic" has one measured guarantee.

Experiment only on fixtures/copied homes:

1. Compare current Luna proposer/Luna critic with at least one model-separated critic configuration.
2. Use labeled extraction, relation, topology, and known over-composition cases.
3. Measure false accepts, false blocks, disagreement utility, latency, and relative cost.
4. Choose:
   - model-independent critic; or
   - explicit same-model role/prompt critic plus Sol sampled auditor as the independent backstop.
5. Align provider doctor severity, policy labels, specs, and UI.

No fact rebuild is required. No live provider mapping changes without owner approval.

This package blocks REVIEW-2 automatic aging, not REVIEW-1 deferral/admission.

### REVIEW-2 - Safe Deferred Aging

**Outcome:** only explicitly approved mechanical/safe classes can age into action.

Implementation:

- whitelist relation/action classes and minimum confidence by policy version;
- never age contradictions, unsure relations, anomalies, audit failures, missing evidence, blocked cards, or unsafe topology;
- use existing `auto_resolve_after` only after admission/critic rules are explicit;
- apply through individual action ledger paths and sampled audit;
- expose upcoming/aged counts and allow policy rollback.

Exit:

- synthetic clock tests prove hard exclusions;
- every aged item has policy/classifier/reason/inverse;
- audit failure demotes and stops further aging;
- strict mode can disable aging without rewriting facts.

### REVIEW-3 - Relation-Aware Batch Review

**Outcome:** repeated compatible work is fast without losing per-item reversibility.

API:

- one batch preview endpoint;
- one batch decision endpoint returning per-item success/failure and a batch undo handle;
- homogeneous kind/relation/outcome enforced server-side.

UI:

- selection shows relation/outcome summary and exact effect;
- incompatible selection disables batch;
- partial failures restore only failed cards;
- batch undo dispatches guarded per-item inverses.

Exit:

- duplicate/supports/complementary work cannot mix;
- one failure cannot mark the batch complete;
- every item remains independently auditable;
- keyboard-only batch acceptance passes.

## R4 - Complete Native Knowledge Workflows

### WIKI-1 - Native Wiki

Implementation order:

1. add `swift-markdown`/cmark dependency and fixture renderer;
2. replace line-based `displayLines`;
3. return stable citation/fact markers from API;
4. add provenance popovers and source opening;
5. wire Confirm/Flag;
6. add page contract rail and snapshot diff;
7. replace `facts.prefix(20)` with honest total plus paging/disclosure;
8. add Entity/source deep links.

Exit:

- Markdown fixture coverage for headings, lists, links, code, emphasis, escaping;
- every fact reveals quote/source;
- Confirm/Flag round trip;
- no silent fact cap;
- light/dark/minimum-width UI suite green.

### ENTITY-1 - Native Entity Actions

Implementation:

- replace rationale-only merge candidates with structured direction/evidence/risk cards;
- add "Propose Merge" through `/api/entities/merge`;
- reconcile proposal into standard Queue/action state;
- add alias/status commands only where existing service primitives and inverses exist;
- add links between entities, facts, pages, and Queue items.

Exit:

- one proposal produces one candidate-key action;
- Swift never writes entity tables directly;
- stale/already-merged result is human-readable and idempotent;
- long names/aliases render correctly.

### ASK-1 - Retrieval Inspection

Implementation:

- expose suppressed candidates and debug trace behind disclosure;
- preserve explicit negative verdict styling;
- link facts/pages/entities/chunks to owning native details/source;
- show selection reasons and provider/index degradation;
- retain bounded local history with clear rerun state.

Exit:

- negative controls visibly return no strong match;
- "why is this here?" is answerable;
- suppressed/raw detail is available without dominating normal use;
- all result types navigate when an owner exists.

## R5 - Complete Native Operations

### OPS-1 - Operational Control Plane

Build one unframed operations destination with sections/tabs for:

- scheduler run-now/pause/resume and no-op reasons;
- automation/ingest runs;
- connectors;
- action ledger and guarded revert;
- policy/audit/contracts;
- index/embedding doctor and maintenance;
- sync peer matrix;
- logs;
- runtime versions and retention inventory.

Reuse existing endpoints first. Add API only when a CLI/service primitive exists but has no JSON route. Raw JSON/log detail stays behind disclosure.

Exit:

- supported browser operations have native parity;
- destructive commands require confirmation and show exact result;
- daemon restart recovers state;
- retention/compaction remain dry-run-first.

### SETTINGS-1 - Safe Configuration

Implementation order:

1. convert Brain Home text editing into choose -> doctor -> preview -> confirm -> stop/start -> rollback;
2. connectors;
3. embeddings/index model management;
4. agent/MCP registration and health;
5. sync role/peer roster;
6. diagnostics export;
7. retain autonomy settings as future-only.

Exit:

- UI never displays one home while daemon serves another;
- every setting reports persistence/restart scope;
- failed switch restores previous daemon/home;
- secrets are never shown or written to shared config.

### SHELL-1 - Navigation And Polish

- one command registry drives Command-K, menu commands, and help;
- readable local/relative timestamps with raw disclosure;
- selected destination/item survives transient restart when valid;
- fixed panes and long labels pass minimum-width fixtures;
- icon-only actions have tooltips/accessibility labels.

This lands after functional destinations so the palette/help enumerate real commands.

## R6 - Maintainable Internals

### ARCH-1 - Characterization And Baselines

Before moving code, freeze:

- Queue summary/cards/decision/undo;
- retrieval packets and compaction;
- extraction reports/watermarks;
- action apply/revert;
- page projection;
- API/CLI registration and latency.

### ARCH-2 - Python Cohesive Modules

Extract in this order behind compatibility imports:

1. Queue read model/commands from `ui_server.py`;
2. ingest/retrieval/telemetry services from `service.py`;
3. extraction selection/payload/validation/provider adapters;
4. fact repository/question/routing/page projection;
5. generic action ledger versus typed handlers.

One extraction per PR. No endpoint/schema/CLI behavior change and no measurable latency regression.

### ARCH-3 - Swift Decomposition

Split:

- `QueueView.swift` into store/controller, list, card views, decision/batch/undo, key registry;
- `Models.swift` by endpoint domain;
- shared confidence/popularity/time/provenance components.

Rendered output and accessibility remain unchanged during movement.

### ARCH-4 - Orphan Removal

Start only after characterization:

1. `PlaceholderView`;
2. `BrainUIHandler.write_html`;
3. `queue_action_count`;
4. conservative review of embedding/entity/extraction helpers.

Search dynamic/public consumers, delete coherent groups, and run full build/tests after each.

## R7 - Privacy And Compatibility

### PRIVACY-1 - Forgetting And Redaction

Write and approve a dedicated feature spec before code. It must define:

- request/preview/confirm/audit UX;
- raw, document/chunk, fact/entity, wiki, memory, index, snapshot, telemetry, export, backup, and sync effects;
- tombstone versus proof-of-deletion policy;
- rebuild and remote propagation;
- retention-backup interaction.

Implement only after GROWTH-1/2 expose every copy.

### LEGACY-1 - Wiki Tables

- report rows/references;
- archive/export;
- numbered migration removes base-schema compatibility;
- fresh and representative upgraded schema tests;
- documented rollback limits.

### LEGACY-2 - LaunchAgent Horizon

Retain rollback tooling until app runtime/sync recovery passes the agreed number of stable releases. Then remove primary documentation/help first, followed by implementation in a separate release.

## R8 - Recovery, Role Mobility, And Profiles

No promotion UI before the first three packages pass.

### TOPO-1 - Home-Relative Paths

Inventory/rebase durable absolute paths, refuse ambiguous external references, and prove a restored home can move machines.

### TOPO-2 - Consistent DB Snapshot

Use SQLite backup/VACUUM semantics, checksum/version the snapshot, restore into an isolated home, and run integrity/schema/retrieval/action audits.

### TOPO-3 - Epoch And Mutation Fencing

Add checksummed topology record and monotonic `primary_epoch`. Daemon, CLI, MCP, scheduler, and action writes reject stale epochs.

### TOPO-4 - Handover And Disaster Promotion

Implement planned handover with rollback first, then disaster promotion with explicit recovery point and returning-old-primary demotion.

### TOPO-5 - Profiles

Only after topology identity/fencing:

- profile registry and concurrent daemons;
- per-profile tokens/logs/backups/notifications;
- device-global source claim/routing;
- profile-specific MCP and sync acceptance.

## R9 - New Inputs And Retrieval Research

### EMAIL-1 - Evidence-Only Email

Blocked until the owner revises/approves the email spec. Then:

1. decide Maildir/mbox source and privacy/redaction corpus;
2. capture one snapshot-replaced document per thread;
3. retrieval fixtures/negative controls;
4. only later consider signal-gated extraction, entity link-only, ephemeral drops, and residue caps.

No OAuth/sending/full-corpus extraction in the first release.

### RETRIEVAL-1..N - Isolated Experiments

Run separately:

1. fact vectors as a second stamped collection;
2. query expansion;
3. neighbor expansion;
4. cross-encoder reranking;
5. semantic entity/gardener candidates.

Each experiment gets:

- feature flag/isolated branch;
- baseline and variant eval;
- latency/storage/cost report;
- negative-control and source-grounding gates;
- rollback/removal;
- one decision before the next experiment.

## PR And Release Discipline

Every PR:

- names one work-package ID;
- updates the owning spec/plan status;
- contains focused tests;
- states data/migration/privacy impact;
- preserves compatibility unless retirement is the package;
- leaves unrelated worktree changes untouched.

Every code release:

```bash
uv run ruff check .
uv run pytest -q
swift test --package-path app
scripts/build-app.sh
git diff --check
```

Additional gates:

| Change | Required evidence |
|---|---|
| native UI | seeded UI suite, light/dark, minimum/normal width, accessibility |
| data deletion/compaction | backup, copied-home rehearsal, dry run, explicit approval, integrity/post-audit |
| policy/model/autonomy | provider smoke, owning eval, cost/quality report, no implicit rebuild |
| schema | fresh + upgraded fixtures, idempotent migration, rollback/export posture |
| sync/topology | multi-home acceptance, failure matrix, stale-writer test |

## Stop Conditions

Stop the release and keep the previous state when:

- Queue summary/card invariants disagree;
- a data operation touches unknown/user-managed paths;
- popularity or pinned lineage changes unexpectedly;
- SQLite lock/integrity checks fail;
- eval gates or negative controls regress;
- a UI mutation bypasses existing service/action primitives;
- a topology writer lacks current epoch;
- a live apply was not explicitly approved.

## Program Completion

The immediate program (ranks 1-8) is complete when:

- Queue truth and card safety are enforced server-side and in both clients;
- every review fact exposes an auditable source date;
- rendered native acceptance runs in CI;
- storage growth is bounded across files and SQLite;
- routine review admission stays within configured limits;
- critic assurance is explicit;
- safe aging/batch work is reversible and audit-gated.

The native-product program (ranks 9-12) is complete when all seven destinations meet [App And Operations](../specs/app-and-operations.md) acceptance without relying on browser parity.

Later platform/product programs are separately approved; they are not implied by completing the immediate program.
