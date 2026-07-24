# PKM Brain Architecture Code Guide

**Status:** current code-navigation guide
**Last verified:** 2026-07-21 against the unpromoted temporal-cognition branch based on rollback commit `d5405b9`; the Brain v2 target reaches schema 24 while the installed release remains on schema 21

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
  -> gmail_sync.py -> cache/gmail-mirror/gmail-mirror.sqlite
  -> shadow_trial.py + gmail_operations.py (local queue analysis)
  -> operational_state.py + operational_shadow.py
  -> operational_db.py + operational_migrations.py + operational_budget.py
  -> db/ops.sqlite
  -> operational_briefing.py + operational_today.py
  -> Today briefing, evidence audit, and local feedback

encrypted Gmail archive
  -> Gmail RAW message API -> gmail_archive_source.py
  -> gmail_archive_sync.py -> mail/gmail-archive.sqlite
  -> AES-256-GCM with a Keychain key
  -> daemon-only search_mail/get_mail_thread

daemon.py + automation.py schedule lane-isolated primitives.
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
| facts | `facts` plus exact source spans/quotes, optional predicate validity, optional flattened event time, and revision lineage | `extraction.py`, `temporal.py`, `fact_records.py`, `wiki_facts.py` |
| entity identity | `entities` and `fact_entities` | `entities.py` |
| durable mutation | `cos_actions` plus inverse | `cos_actions.py` |
| autonomy | versioned `cos_policy` and eval state | `cos_policy.py`, `evals.py` |
| Gmail operational mirror | rebuildable immutable revisions/current pointers, tombstones, triage queue, and provider checkpoint | `gmail_mirror.py`, `gmail_sync.py` |
| encrypted Gmail archive | exact RFC-2822 ciphertext plus one archive sync state | `gmail_archive.py`, `gmail_archive_source.py`, `gmail_archive_sync.py` |
| operational current state | `db/ops.sqlite` items, observations, events, and cursors | `operational_state.py`, `operational_db.py` |
| review residue | `open_questions`, proposed memories, audit flags | `wiki_facts.py`, `memory.py`, `ui_server.py` |
| page projection | contracts, facts, synthesis, snapshots, Markdown | `wiki_facts.py`, `contracts.py` |
| retrieval packet | current/valid/known/bitemporal/timeline facts plus eligible evidence layers | `service.py`, `fact_retrieval.py`, `temporal.py` |
| automation | jobs and `automation_runs` | `daemon.py`, `automation.py` |
| sync | source outbox/staging/mirror and `sync_runs` | `sync_*.py` |

## Package Guide

### Workspace And Persistence

- `paths.py`: all home-relative paths, node identity, lock/token/handshake paths.
- `db.py`: base schema, connection helpers, FTS setup, row helpers.
- `migrations.py`: ordered idempotent migrations 1-26 in the unpromoted Brain v2 target; migration 25 adds the immutable Gmail temporal review ledger/head and migration 26 adds durable runner executions/components, including zero-work outcomes.
- `operational_db.py` and `operational_migrations.py`: the independently versioned `db/ops.sqlite` control plane and bounded lock handling.
- `gmail_mirror.py`: the independent owner-only, rebuildable Gmail operational evidence store, atomic local-analysis queue, and content-free poison-thread quarantine at `cache/gmail-mirror/gmail-mirror.sqlite`.
- `gmail_archive.py`, `gmail_archive_source.py`, and `gmail_archive_sync.py`: the separate encrypted raw-message store, Gmail reader, and scheduled 90-day-plus-incremental sync.
- `config.py` and `sync_config.py`: local/shared and role-specific config.

Do not create ad hoc paths or SQLite connections in UI/Swift code.

`brain.sqlite` remains authoritative for knowledge. `ops.sqlite` is authoritative only for current operational state. The Gmail mirror is rebuildable operational evidence. The encrypted Gmail archive is a separate local evidence store and is not joined into those databases.

### Capture And Ingest

- `connectors.py`: connector registry, manifests, config, health.
- `connector_auth.py`: OAuth provider metadata, state/PKCE/nonce flow, loopback callback, and Keychain storage.
- `connector_api.py`: bounded connector command routing for the JSON API.
- `capture.py`: built-in agent/note capture adapters and snapshot export.
- `gmail_knowledge.py` and `gmail_projection.py`: encrypted-archive-to-Knowledge rendering, immutable projection identity, projection-v7 ordered per-message admission/advertising/relevance authority, owner-only artifact capture, and revision reconciliation.
- `source_dates.py`: trusted Gmail file/lineage/message timestamp-range and projection-v7 per-message policy validation; sender-controlled headings and dates never become trusted clocks.
- `service.py`: workspace initialization, source detection, ingest, chunk/index writes, quarantine, status.
- `chunking.py`: deterministic chunk boundaries and evidence text preparation.

Connector output stops at `inbox/`. `BrainService.ingest` is the single entry to raw/document/chunk/index state.

The manual Calendar/Shadow evaluation path and scheduled Gmail mirror are not capture output. They use separately bound read-only credentials through the operational modules below and never write `inbox/`, documents, chunks, facts, or wiki pages.

### Embeddings And Indexes

- `embeddings.py`: config resolution, hash/ST providers, unavailable sentinel, query/passsage interface.
- `indexes.py`: LanceDB tables, provider stamp checks, vector rebuild/backfill, index statistics.
- SQLite FTS definitions live in `db.py` and service helpers.

Provider mismatch must disable/refuse the vector channel; never mix spaces or silently substitute hash.

### Facts, Entities, And Pages

- `extraction.py`: document/window selection, prompts, evidence units, validation/retry, watermarks, critic/conflict helpers.
- `extraction_contract.py`: complete v12 response schema, fixed enums, and prompt-version identity; older extraction watermarks are intentionally revisited once.
- `extraction_entities.py`: fail-soft entity-annotation normalization; malformed optional mentions cannot discard an evidence-backed base fact.
- `event_projection.py`: deterministic, evidence-backed projection of trusted source-native meeting times into ordinary event facts.
- `gmail_temporal_discovery.py`: deterministic, message-local Gmail temporal expression discovery with exact evidence spans and explicit resolution bases; output is a non-routable sidecar lead.
- `gmail_temporal_leads.py`: content-free, snapshot-bound inventories of temporal expressions and event/action/lifecycle mentions plus bounded review-only association hints. Expressions preserve calendar dates, local wall times, local segment identity, ambiguity, blockers, and exact source offsets without pretending an unzoned time is an instant. Recurrence and unresolved coarse-relative forms remain unresolved; message-anchored coarse phrases may preserve a calendar day while leaving time of day unresolved. Conservative structured event titles are review-only candidates. Quoted and forwarded evidence stays inventoried but is provenance-blocked and deferred. Fact admission and explicit temporal rescue may authorize association hints, but neither changes the evidence inventories; positive rescue associations remain low-confidence, deferred, and non-routable.
- `gmail_temporal_batching.py`: pure deterministic partitioning of one analysis into one-expression, segment-local selector packets with exact source slices, an optional subject-line event bridge, hard byte/endpoint ceilings, complete expression coverage-or-omission accounting, and a content-bound endpoint manifest. A trimmed prose window never becomes a recall boundary: exact endpoints from the expression's complete deterministic sentence segment remain eligible, while adjacent-segment padding grants no authority and overflow stays explicit. It makes no model call or write and rejects citations outside the exact packet authority.
- `gmail_temporal_frontier.py`: complete validator-derived candidate enumeration, alias clustering, lossless bounded paging, exact verdict coverage, three-run consensus, and non-authorizing uncertainty/semantic-split review.
- `gmail_temporal_review.py`: pure message-level projection of supported citations, uncertainty sidecars, split-semantic reviews, alternatives, and reschedule structure; every output remains non-routable. Review v3 keeps verifier-selected subject mentions unchanged as evidence, then separately carries the complete source-proven alias family and a canonical title only when exactly one `event_title_candidate` exists for an event-centered hypothesis. Multiple titles, mixed families, and non-event subjects leave the canonical identity null instead of broadening verifier support.
- `gmail_temporal_event_identity.py`: pure bounded event-unit identity bridge with revision-stable content anchors and exact authority-bound comparison plans. Projection-carried subject types are rebound to aligned, immutable, content-free lead analyses before eligibility or binding; the analysis snapshot receipt is recomputed from every retained field plus the trusted source hash, canonical message clock, and production message-scope key. The v2 thread authority also binds the current ledger's exact analysis fingerprint, projection fingerprint, and canonical projection SHA-256, so jointly refingerprinting a projection and analysis cannot be smuggled through an unchanged head receipt. Only source hypotheses whose authoritative types are `event`, `event_title_candidate`, or `event_predicate` enter this layer. Action, boundary, and deadline observations remain review artifacts without being eventized. Every isolated unit receives a deterministic source-bound provisional identity, including units with uncertain pairwise coreference, while a no-pair plan requires zero external verdicts. Cross-unit reconciliation still requires three complete external verdict sets and a unanimous clique. Each cluster records one exact key-anchor unit: root resolutions use the canonical first component unit, while updates may retain that key through the exact prior-resolution fingerprint bound by the trusted thread authority. This accepts legitimate multi-generation lineage after an earlier historical message arrives while rejecting a modified or substituted parent receipt. The public binder revalidates the analyses, complete plan, exact authorized parent, and assertion-to-unit mapping before assertions reach lifecycle projection. A durable loader and append-only identity-head ledger must supply these receipts in production; constructing the pure authority dataclasses is not a hostile-process security boundary. Because this layer also has no trusted owner-receipt ledger, it rejects inputs that already carry `owner_verified` assertions instead of silently dropping or downgrading their lineage. Identity units and plans are v3 and bind exact verifier evidence, deterministic aliases, and the optional canonical title; pair, verdict, cluster, and resolution schemas remain v2. The module performs no provider call or persistence and every output remains deferred and non-routable.
- `gmail_temporal_event_identity_consumers.py`: transient source-bound consumers for identity v3. It rebuilds the exact plan, reloads each trusted source hash and mention span, and produces bounded sanitized pair requests in which verifier-selected evidence and deterministic identity metadata are explicitly separate. After a validated resolution, it exports retrieval bindings only from qualified canonical titles; generic heads such as `meeting`, `review`, or `session`, ambiguous title families, and stale source authority produce no alias. It prepares no provider call, persists no private text, and does not make any result routable.
- `gmail_temporal_thread_lifecycle.py`: pure cross-message review projection over one immutable Gmail thread snapshot. Every resolver-verified assertion requires the complete content-free analyses, exact plan, resolution, and declared parent; the lifecycle payload retains and revalidates them, so callers cannot bypass the resolver with hand-built assertions. It preserves every scheduled occurrence and lifecycle source. A validated `source_bound_self_identity` authorizes only that observation's provisional single-source view; it never proves that another observation is the same or a different event. Reschedule, cancellation, and completion across observations require one externally verified shared key, while owner authority fails closed pending a trusted owner-receipt ledger and a standalone terminal observation remains unresolved instead of inventing schedule history. Missing, conflicting, or implicit identity/state changes remain unresolved alternatives. Thread membership or a matching date never establishes event identity, and every derived v3 view remains deferred and non-routable.
- `gmail_temporal_runner.py`: authoritative post-ingest reconstruction of trusted target-message evidence and policy, deterministic analysis/batching/frontier/pages, exact three-component validation, ensemble/projection, and durable zero-work outcomes. It is the intended production-scope caller of the review ledger, pins external Codex Luna medium, and does not itself launch a provider.
- `gmail_temporal_persistence.py`: source-bound append-only review runs, artifacts, execution/component receipts, idempotent compare-and-swap current heads, stale detection, atomic clear, and rollback. Production heads without a matching complete execution receipt are retired during migration and fail closed on reads. Review projection v3 makes persisted v1/v2 heads stale while retaining them as immutable compare-and-swap history; rollback cannot resurrect a retired schema. Execution evidence is a validated in-process capability and provider execution remains self-reported, not cryptographically attested; a malicious caller already executing inside the Brain process can fabricate that capability. It cannot write facts, events, reminders, or operational state.
- `gmail_temporal_selection.py`: the endpoint-only semantic authority boundary. An external selector may cite an expression, an event/action/title subject, an optional lifecycle cue, and an optional matching lead; deterministic code derives normalization, relation, planned/actual kind, lifecycle, confidence, blockers, and deferral. Terminal boundaries are allowed only as deferred, semantically unspecified subjects. Snapshot fingerprints and content-bound IDs reject stale responses, and every result remains non-routable.
- `gmail_temporal_reduction.py`: offline reduction of independently selected packets. It revalidates every manifest and association in isolation, preserves valid siblings when one citation is bad, deterministically deduplicates/ranks/caps the surviving set, and forces deferral for omissions, conflicts, invalid rows, positive rescue associations, or overflow. Diagnostics are immutable and content-free.
- `gmail_temporal_evaluation.py`: read-only current/history replay, aggregate recall diagnostics, frozen HMAC-opaque calibration cohorts, complete-label/stratification gates, and determinism checks for the private Gmail projection.
- `scripts/audit_gmail_temporal_runner.py`: local preparation-only full-corpus audit with aggregate counts, static error buckets, bounded policy strata, and volume percentiles; it prints no message/path/identity/hash/payload content and makes no model or persistence call.
- `scripts/diagnose_gmail_temporal_event_identity.py`: public-fixture-only structural diagnostic for zero-call singleton addressability, event/non-event gating, uncertain-pair isolation, unanimous cross-unit identity, exact resolver-to-lifecycle authority, and non-routability. Its counts are structural coverage evidence, never semantic recall or precision.
- `scripts/freeze_gmail_temporal_development_baseline.py` and `scripts/build_gmail_temporal_holdout.py`: authenticate the all-current development thread scope, then freeze exactly 150 natural primary messages, 100 balanced challenge messages, and 75 sealed reserve messages. Primary and challenge evidence scopes, development overlap, and prospective-unseen status are recorded separately; the natural operability and challenge stress-recall estimands may never be pooled. No-reroll enforcement is scoped to one retained owner authority root and is not represented as a global or independently reverified guarantee.
- `scripts/finalize_gmail_temporal_holdout_labels.py` and `scripts/prepare_gmail_temporal_holdout_candidate_gold.py`: validate prediction-blind Sol-medium source labels, complete cohort coverage, label-authority receipts, candidate alignment, and exact source/evaluation commitments. Model labels are not called human gold, and neither stage claims a production release.
- `scripts/run_gmail_temporal_holdout_external.py`: the only network-capable holdout utility. It labels source-only queues with restricted ephemeral Sol medium before opening verifier authority, then executes bounded Luna-medium verifier batches with owner-only resumable call receipts. Its truthful v2 run attestation records every external call and retry; request, response, receipt, partition, checkpoint, protocol, and source-module hashes are recomputed before scoring. It never makes a label or verifier result routable.
- `scripts/build_gmail_temporal_public_challenge_v3.py`, `scripts/audit_gmail_temporal_public_gold.py`, and `scripts/run_gmail_temporal_public_challenge.py`: freeze, independently audit, execute, and score a public-synthetic, non-release semantic challenge without private Gmail. The freezer retains missing and zero-work positives and writes content-free frontier coverage to a separate owner-only HMAC sidecar; the prediction `run` phase accepts neither gold nor either audit artifact. The gold auditor writes the exact approved fixture into its owner-only evidence root, then records an authenticated plan, detail, summary, and every bounded request, response, and receipt from the prediction-blind restricted external `gpt-5.6-sol` medium audit. Score v13 accepts this evidence only through `--gold-audit-root`; it pins the fixture-generator version and source SHA-256 plus the audit contract, verifies exact call and case coverage, binds every artifact hash and aggregate, enforces plan-to-call-to-detail-to-summary chronology before the prediction plan, and requires zero corrections and no local/test execution. Missing, extra, tampered, or inconsistent evidence fails closed; omitting the root preserves historical scoring but makes the personal target unavailable. The scorer authenticates exact prior-launcher artifacts and independently recomputes frontier member coverage from current production candidates and exact gold identities, rejecting a sidecar that merely claims coverage. It reports relation/lifecycle strata, coherent reschedule units, canonical-title and canonical-member recall, accepted-set Jaccard, exact critical-candidate confidence agreement, and per-category critical-gold-member confidence agreement. The personal-use gate requires 95% frontier and pooled critical recall, 95% recall in every scheduled/cancelled/reschedule/deadline class and for coherent reschedule units, 90% final/confirmed/unit/canonical recall, 95% supported precision, 90% review precision, 80% candidate-bearing-negative rejection, 0.95 accepted-set and critical-confidence stability, complete lifecycle/relation reporting, restricted execution, zero positive no-work cases, zero forbidden hypotheses, zero wrong critical artifacts, and zero supported critical calibration overclaims. Ordinary overclaims and selected negatives remain stricter smoke diagnostics. A separate outer gate requires a release-eligible independent `blind_first_use` cohort, so a transformed development replay can demonstrate quality without being mislabeled production evidence. Legacy gold and prediction evidence remain immutable; v13 retains authenticated historical-launcher acceptance and never rewrites an old label or prediction.
- `scripts/evaluate_gmail_temporal_candidate_gold.py`, `scripts/score_gmail_temporal_holdout.py`, `scripts/audit_gmail_temporal_holdout.py`, and `scripts/evaluate_gmail_temporal_retrieval_holdout.py`: candidate evaluation v2 applies the same confidence-neutral definition to effective semantic recall and artifact precision while retaining confidence-sensitive supported precision, confirmed recall, and overclaim gates. The holdout stages score natural operability and balanced capability separately, reject legacy single-call verifier attestations, freeze a 25%-plus-all-errors owner audit for each cohort, and evaluate retrieval against cold blind queries without exposing context, taxonomy, thread scope, or relevance gold to the retriever. These artifacts are promotion prerequisites; only a later rollup that binds parity, trusted retrieval, both owner audits, and a completed seven-day/300-message canary may claim promotion.
- `scripts/export_gmail_fact_parity_admissions.py` and `scripts/build_gmail_fact_parity_cohort.py`: freeze a separate, thread-disjoint identical-packet fact-rich cohort for original-Brain versus V2 capability parity. The temporal cohorts cannot substitute for its at-least-50-unit/30-thread denominator, and its fact, thread, and macro-thread scores are never pooled with temporal metrics.
- `scripts/build_gmail_fact_parity_cohort.py`, `scripts/gmail_fact_parity_contract.py`, `scripts/run_gmail_fact_parity.py`, and `scripts/evaluate_gmail_fact_parity.py`: HMAC-bind raw sources to opaque packet identities, define and enforce one-to-one evidence-derived candidate/action/fact ownership, and bind production tree/runtime/prompt/invocation provenance for original-Brain parity. Structural metrics and the production release gate are separate. The fail-closed cross-version orchestrator is implemented; its canonical production extraction adapter and independently authenticated invocations remain required before these schemas become release evidence.
- `scripts/build_gmail_temporal_retrieval_source_authority.py`, `scripts/run_gmail_temporal_retrieval.py`, `src/pkm_brain/retrospective_retrieval.py`, and `BrainService.retrieve_retrospective_evidence`: build a complete HMAC-opaque active-Gmail authority, authenticate its private source/message/thread and exact chunk-inventory binding, and run the exact public read-only source/temporal retrieval route over a disposable index snapshot. The source-availability cutoff is distinct from `known_as_of`; temporal intent comes only from the user query; results are never padded; incomplete, unmapped, or future fact citations fail closed; and the execution receipt binds the exact implementation, protocol, and snapshot commitments.
- `scripts/audit_gmail_temporal_shadow_canary.py`: local-only HMAC baseline/observation audit for new-thread versus existing-thread-unseen messages. Its complete state envelope is authenticated; an owner-only lock plus authenticated generation compare-and-swap serializes updates; production duration uses only the wall clock; and explicit deterministic clocks permanently mark their state non-release. It measures structural progress without provider, model, persistence, or Brain writes and keeps every semantic release gate false until owner-confirmed materiality, precision/recall, scheduled execution, and cross-message lifecycle reconciliation exist.
- `event_routing.py`: event-page coherence plus Gmail's evidence-bound temporal-integrity checksum; application reloads exact cited chunks and requires the recomputed clock, primary event, and complete stabilizer audit before accepting event time.
- `temporal_enrichment.py`: fail-open stripping/reporting for optional predicate/event time and event-aware candidate deduplication.
- `fact_event_integrity.py`: persistence-time event-entity enforcement and temporal merge guards.
- `fact_relations.py`: typed candidate/counterpart relation classifier.
- `numeric_faithfulness.py`: cited-number parsing and support checks, excluding speaker identifiers and non-count existentials.
- `temporal.py`: independent validation for optional predicate validity and the single-event `event_time` shape, clock predicates, request modes, interval comparison, and conservative succession gates. Temporal validation may discard enrichment but may not reject a valid base fact.
- `fact_records.py`: exact fact-row serialization, revision metadata, lifecycle status, and inverse snapshots.
- `fact_retrieval.py`: paged FTS fact eligibility, historical-status handling, lineage deduplication, and timeline ordering.
- `source_evidence.py`: source-date and source-document evidence normalization shared by review projections.
- `routing_coherence.py`: source-document routing priors used without overriding contradictory fact evidence.
- `entities.py`: normalization, resolution, type/mention gates, aliases, link helpers.
- `wiki_facts.py`: fact persistence/lifecycle, open questions, routing, page projection, snapshots, human answers.
- `contracts.py`: page-scope and retrieval-purpose contracts.
- `gardener.py`: deterministic topology candidates, per-candidate LLM disposition, proposal.
- `regeneration.py`: source-driven rebuild workflow and safety/reporting.

Entity identity is not a page path. `fact_entities` is link authority; `facts.entity_id` is a primary-link cache.

Fact actions use copy-before-write revisions. The current row keeps its stable fact ID; before a semantic update, its exact row and entity links are copied to a closed `factrev_*` history row, then the stable head advances to the next open revision. Migration 23 supplies assertion lineage, predecessor, revision number/status, and one-open-revision constraints. Migration 24 adds nullable `event_time_kind`, `event_start_at`, `event_end_at`, `event_time_precision`, and `event_time_expression` columns; the extractor/API exposes them as one nested object tied to exactly one primary event entity. Closed revisions remain searchable for temporal retrieval and reversible through the existing action inverse.

### Actions, Policy, Audit, And Evals

- `cos_actions.py`: action type registry, proposal/application, inverse capture, guarded revert, sibling retirement.
- `critic_context.py`: bounded, source-grounded cross-chunk entity attribution context for critic review.
- `policy_action_batch.py`: parallel critic preparation with ordered, independently committed action finalization.
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
- `google_api.py`, `google_sources.py`, `google_normalization.py`, and `google_routes.py`: bounded read-only Google transport, resumable Calendar/Gmail change reads, deterministic normalization, and safe provider routes. Gmail full bootstrap uses `newer_than:7d -in:spam -in:trash`; later reads use Gmail history.
- `google_cache.py`: owner-only disposable raw and revision-addressed normalized evidence with separate retention lanes.
- `gmail_mirror.py`: private mode-`0700` mirror directory, owner-only SQLite/WAL/SHM, immutable sanitized revisions, current pointers, provider-confirmed tombstones, durable triage leases, and atomic mailbox checkpoints; no attachment bytes or knowledge ingestion.
- `gmail_sync.py`: policy/grant-gated Gmail API bootstrap/history synchronization and crash-atomic mirror acceptance, with bounded expired-history resync that preserves prior state and never deletes by absence.
- `gmail_llm.py` and `gmail_operations.py`: restricted Gmail detector boundary, local-queue-only bounded structured detection with no Gmail calls, deterministic evidence/lifecycle validation, and source-local handled assessment.
- `operational_budget.py`: durable local-day API/call/token reservations.
- `operational_shadow.py`: shadow runs, decisions, handled assessments, and missing reports.
- `shadow_trial.py` and `shadow_controller.py`: manual Calendar refresh plus local Gmail analysis/reconciliation, coverage, retention, and polling state; Gmail provider checkpoints are owned separately by `gmail_sync.py`.
- `operational_briefing.py`, `operational_today.py`, and `today_presentation.py`: deterministic briefing projection, ignored/suppressed audit, evidence navigation, and local feedback contracts.
- `paths.py`: `BrainPaths.ops_sqlite_path` and `BrainPaths.gmail_mirror_sqlite_path` resolve the operational database and private rebuildable mirror inside the same Brain home.

The provider-read-only/local-write slice now separates scheduled Gmail transport from manual analysis/evaluation. `gmail_mirror_sync` runs only on `single|primary`, at startup and about every 600 seconds on the dedicated `provider_sync` scheduler lane, after exact policy/content-approval/account/scope gates pass. It safely restarts rejected saved page tokens, preflights the bounded worst-case request envelope, and quarantines a malformed/oversized/conflicting thread without blocking valid peer revisions or checkpoint progress. A complete normal checkpoint may be followed by one second generation that retries at most ten due quarantine thread IDs with durable exponential backoff and parser-version bypass; retry-only failure leaves mailbox freshness intact and analysis partial. Mailbox checkpoint freshness, scheduled-job health, and analysis/quarantine backlog are independent. Fresh live mirror validation, cross-source episode linking, production trust, and guarded execution remain gated by [Chief-of-Staff Operations](specs/chief-of-staff-operations.md).

### Encrypted Gmail Archive

`gmail_archive_source.py` fetches exact Gmail `RAW` messages. `gmail_archive_sync.py` runs one state machine: a resumable fixed 90-day backfill followed by Gmail history incrementals; an expired history cursor restarts the full scan without deleting accepted ciphertext. `gmail_archive.py` encrypts raw messages and parsed text with AES-256-GCM using a Keychain-backed key. V1 search decrypts and scans locally rather than maintaining another search index.

The archive retains local ciphertext when Gmail reports deletion. It never feeds the operational detector, Luna, Brain documents, facts, or default retrieval. Agents can reach it only through bounded daemon-backed `search_mail` and `get_mail_thread`; the direct MCP fallback does not expose either tool.

### Retrieval And Memory

- `service.py`: search, layer selection, reranking, verdict/calibration, context packet, lineage, telemetry compaction.
- `memory.py` and `memory_proposals.py`: typed memory lifecycle and model/deterministic proposals.
- `mcp_server.py`: small stable agent-facing tool surface.

Retrieval exposure is telemetry. It can order review work but cannot alter truth confidence.

### Automation And Operations

- `automation.py`: capture/nightly stage orchestration and summaries.
- `daemon.py`: loopback API process, lane-aware scheduler registry (including isolated `provider_sync`), token/lock/handshake, parent monitor.
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

The mail-archive tools are an exception to the normal MCP fallback model: they require the authenticated daemon, return bounded untrusted evidence, and are unavailable when the daemon or archive is unavailable.

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

When changing the encrypted Gmail archive:

1. keep raw and parsed mail encrypted outside Brain and operational databases;
2. preserve the single backfill-to-history state machine and expired-cursor restart;
3. keep `search_mail` and `get_mail_thread` bounded and daemon-only;
4. verify progress reporting, provider-deletion retention, and zero LLM/fact admission.

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
