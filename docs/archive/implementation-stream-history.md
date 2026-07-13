# Implementation Stream History

**Status:** compact historical summary
**Last verified:** 2026-07-13 against release `0.1.2` snapshot `42c44c7`

This file preserves the durable outcomes of earlier implementation-stream specs without retaining thousands of lines of completed phase instructions. Full prior documents remain in git history.

## Durable Decisions

- Raw evidence, facts, entities, actions, managed pages, memories, and indexes have distinct authority.
- Ingest stays deterministic and offline-capable; LLM curation is post-ingest and role-configured.
- LLM judgment is separate from deterministic, reversible mechanics.
- Human review is factual/action residue, not a queue of generated Markdown diffs.
- Eval gates precede autonomy promotion; sampled audit can demote/revert.
- The primary owns canonical sources; children export source artifacts and rebuild local derived state.
- Native macOS is the primary UI; the shared-API browser remains a fallback.
- Current-state docs are feature-oriented. Completed build chronology belongs here or in git history.

## Timeline

### June 2026 - Foundation And Retrieval

- The original V0.1 product spec established local-first storage, source/chunk retrieval, MCP memory, wiki projections, privacy, and sync goals.
- Retrieval tuning added explicit `found|partial|no_strong_match` verdicts, fact relevance floors, source-aware noise controls, provenance-aware source-hit metrics, calibration, negative controls, and repeatable evals.
- Chief-of-Staff work introduced the fact ledger, reversible action spine, versioned policy, page contracts, synthesis storage, open-question residue, eval gates, and audit loop.

### Late June - Entities And Determinism

- Entity identity was separated from page routes.
- Migrations 18-19 added entities, many-to-many fact links, primary-entity cache, and mention kind.
- Merge/split became reversible action operations with type guards.
- The project standardized deterministic mechanics versus gated semantic decisions and required current docs to state verification status.

### July 1-6 - Extraction, Routing, Embeddings, And Autonomy

- Extraction moved from model-authored quote copying to evidence-unit citation with deterministic spans and quote caches.
- Low-information windows, closed claim classes, normalized-content watermarks, numeric faithfulness, direct-entailment prompting, and bounded parallelism reduced waste and provenance failures.
- Routing hints became per-window and canonical-only; reference/log destinations were blocked and fuzzy canonical snapping was added.
- Gardener judgment became per-candidate with isolated failures, auditable drops, and risk-based effort.
- Sentence-transformer embeddings became config-driven, stamped, explicitly downloadable, rebuildable, and non-silently degradable. The live Brain moved to BGE after an eval-gated comparison.
- Critic/auditor gates and policy promotion enabled reversible low/medium-risk autonomy while retaining hard human boundaries.

### July 7-8 - UI, Daemon, App, And Sync

- Browser UI v2 established job-oriented Today, Queue, Wiki, Entities, Ask, and Ops surfaces over a shared loopback API.
- The app-managed daemon replaced normal LaunchAgent operation with a serial scheduler and one sync job per peer.
- The SwiftUI app, runtime provisioner, supervisor, menu bar, migration/rollback, shims, and MCP proxy landed.
- Primary and secondary Macs migrated to the app runtime. Real two-machine sync completed successfully on July 8.

### July 9-10 - Review Integrity And Volume

- Queue paging, complete policy/topology cards, candidate-key deduplication, stale merge handling, readable errors, and native review mechanics landed.
- The typed relation flow and approved W2a reconciliation closed 447 safe review questions and reduced the visible Queue from 554 to about 105.
- Retrieval exposure became an advisory sort for Queue, entities, and facts.
- Confidence gained accessible High/Medium/Low bands.
- Settings gained Review First, Balanced, and More Autonomy modes that affect future actions only.
- Active Codex evaluator/gardener flows moved from 5.4/5.4-mini to `gpt-5.6-luna` with task-relative effort. The former 5.5 auditor moved to `gpt-5.6-sol` xhigh with Luna fallback.

### July 11 - Reconciliation, Growth Controls, And Native Completion

- Pairwise conflict repair released 82 non-conflicting legacy cards; policy reconciliation applied 104 supported facts and rejected 62 unsupported candidates; topology reconciliation reduced 205 old rows to 11 current merge/split reviews.
- Extraction-evidence and audit repairs removed stale/non-actionable findings, restored source context, and left only reviewable current actions.
- Review admission became durable in schema 21: existing work is grandfathered, ordinary future work is limited to 25 admissions per day and 100 active items, hard boundaries bypass limits, and Deferred remains visible.
- Retrieval and automation payloads gained write-time size limits; test/eval retrieval no longer inflates popularity; managed storage inventory classifies runtime, backups, database, indexes, and logs.
- Runtime `0.1.1` gained immutable IDs, model-cache reuse, process-aware retention, a verified `/Applications` installer, macOS CI, and isolated rendered XCUITest coverage.
- Public CI run `29184839657` passed 472 Python tests, 17 Swift tests, the release build, and seven-destination XCUITest on macOS 15/Xcode 16.4. The runner exposed and verified the fix for a notification completion handler that had inherited main-actor isolation on a private callback queue.
- Native Wiki moved to `swift-markdown` with evidence and fact actions; Entities gained policy-gated merge proposals; Ask gained debug/source navigation; Ops gained scheduler, runs, connectors, and storage; Settings gained rollback-safe home switching.
- A line-count ratchet now prevents growth in the six largest Python/Swift modules. `PlaceholderView`, `BrainUIHandler.write_html`, and `queue_action_count` were removed as confirmed orphans.

### July 12-13 - Topology Control And Queue Follow-Through

- Settings gained a future-job topology review threshold, while native/browser Inbox custom routes gained substring autocomplete over the sanitized routable-page pool.
- Applied entity-merge audits now recognize active-destination/merged-source state; unapplied merge proposals whose targets later become inactive are excluded as stale rather than shown as blocked.
- A missing low-risk topology policy rule had caused safe sub-threshold merges to fall through to default L3. Policy v14 added explicit L2 critic coverage without weakening size, cross-type, confidence, critic, or eval boundaries.
- A dry-run-first live reconciliation inspected 26 merge candidates, closed 6 stale candidates, suppressed 1 overlap, rejected 2 through fresh gardener judgment, applied 13 through L2 critic review, retained 4 L3 decisions, and completed with zero failures. The resulting live Queue contained 65 total items.

### July 13 - Source Dates And Inbox Autonomy

- Queue fact dates now prefer source-native event start, source creation, capture, and document-ledger time before fact observation, preventing a July reprocessing timestamp from appearing as the date of an April Hightouch meeting.
- Future extraction requires explicit extraction, routing, and truth confidence; historical clean quote-backed `0.5` defaults are handled by a separate critic-required policy rule rather than treated as genuine model uncertainty.
- The second-stage route resolver gained same-source context, canonical-page creation, active autonomy floors, compact-index/full-prompt retries, malformed-output rejection, and named-organization route guards.
- Policy v15 reconciled 22 historical confidence escalations: 12 applied and 10 were rejected by the normal critic path with no failures. Policy v16 then applied 209 reversible fact rehomes from 11 legacy Inbox batches, leaving no batch or individual routing residue.
- A complete audit of the 209 routes corrected one Snowflake-to-Databricks semantic error and consolidated avoidable Snowflake, Greylock, and Orchid page fragmentation. Managed Wiki projection archived the superseded empty Snowflake culture page.
- The repaired resolver then reclaimed the final standalone Maestro/Dagobah card into the same-source Netflix data-product page; critic review agreed, leaving the live actionable Queue with only two Peter/Peter-Wang topology merges.
- Fresh topology reconciliation dropped the old Decagon Labs/Decagon name-containment proposal after finding plausible distinct scope, retained one high-risk Peter page merge, and produced no deferred topology residue.
- Production acceptance exposed and fixed three independent nightly faults: a 1.79-million-character auditor request, parallel critic workers competing during fact writes, and one malformed extractor window aborting the stage. Auditor cards now cap at 48,000 characters and batches at eight actions/180,000 characters; critic calls remain parallel but writes finalize serially; malformed provider output records an invalid retryable watermark.
- Nightly run `automation_7b91433093b14d52` then completed successfully. Sol-medium audited all 25 samples in four batches (16 OK, 9 bad), state-aware review retained one applicable finding, and the live Queue measured eight actionable items with zero Inbox residue.

## Superseded Audit Items

The July 7 audit correctly identified packet bloat, duplicate citation fields, failed-nightly visibility, dead golden-query configuration, shallow memory audit, source re-hashing, monolithic modules, storage growth, and documentation sprawl.

By July 10 these were fixed:

- compact context packet and duplicate citation removal;
- local golden-query fixture loading;
- migration 20 source-stat ingest shortcut;
- duplicate/stale/unresolved/superseded memory audit checks;
- Queue integrity and review-volume reconciliation.

Still active after `0.1.1`:

- decomposition of the guarded large Python/Swift modules;
- remaining native Ops/Settings and shell parity;
- broader light/dark/minimum-width accessibility coverage;
- explicit legacy schema/LaunchAgent retirement;
- critic assurance, safe aging, and relation-aware batching;
- topology mobility/profiles;
- revised email connector;
- eval-gated retrieval experiments.

See [the current audit](../audits/project-audit-2026-07-10.md) and [improvement plan](../plans/project-improvement-plan.md).
