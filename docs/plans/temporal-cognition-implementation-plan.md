# Temporal Cognition Implementation Plan

**Status:** T0-T5 implemented and evaluated in isolated Brain v2; T5A evidence-first Gmail discovery, apply-time provenance, and historical replay are complete; T5B now includes review-only expression batching, temporal rescue, event-title candidates, and association-isolated reduction, but human-grounded calibration, live lifecycle validation, optional backfill, and controlled promotion remain open
**Last verified:** 2026-07-21 against the unpromoted temporal-cognition branch based on baseline commit `d5405b9`; the content-safe replay preserved current fact-admission parity, the bounded current-source replay covered all 265 important-temporal proxy threads, and the full 1,297-test suite passed while promotion remained blocked on accuracy evidence
**Owning specs:** [Capture And Knowledge](../specs/capture-and-knowledge.md), [Curation And Review](../specs/curation-and-review.md), and [Retrieval And Memory](../specs/retrieval-and-memory.md)

## Objective

Add useful temporal cognition without weakening the original Brain's durable-fact coverage, routing, entity identity, lifecycle behavior, or default retrieval.

Success means:

- the base fact extractor keeps the original atomic claim, evidence, entity, routing, and confidence gates;
- all facts participate in source-observation and Brain-knowledge clocks;
- predicate validity is optional and is added only when the proposition itself has an explicit world-valid interval;
- a fact primarily about one named event may carry one optional `event_time` object;
- malformed, absent, or ambiguous temporal enrichment never rejects or hides an otherwise valid fact;
- explicit historical queries work without changing results for callers that omit temporal controls;
- updates, corrections, contradictions, plans, and actual occurrences remain source-backed and reversible.

## Non-Goals And Invariants

- No canonical `events` table, graph database, timeline database, or generic temporal-relation store.
- No required temporal classification for every fact.
- No duplicate fact-extraction pipeline, daemon, scheduled job, provider, or model role.
- No automatic conversion of operational Calendar items in `ops.sqlite` into durable facts.
- No processing-time fallback for `observed_at`. Inferred-year or relative expressions may be normalized only against the trusted source-message clock with an explicit resolution basis, and remain review-only until their own historical calibration passes.
- No temporal parse failure may become a base-fact validation failure.
- No default-retrieval recall loss is acceptable.
- No destructive legacy cleanup in this tranche.
- Every semantic fact mutation remains an existing policy decision recorded in `cos_actions` with an exact inverse.

## Target Architecture

### 1. Base Fact First

Extractor v12 proposes the same durable fact as original Brain: statement, evidence-unit references, claim class, entities, route, and extraction/routing/truth confidence. Deterministic validation decides whether that base fact is admissible before considering optional annotation and temporal enrichment. Unsupported durable claim labels and malformed entity annotations fail soft so an evidence-backed base fact survives.

Temporal parsing is a subordinate step. Evidence-first discovery may identify exact temporal spans before or independently of model extraction; deterministic normalization owns bounds and provenance, while the model may only associate cited spans with a named event and actual/planned semantics. If discovery, association, or validation fails, Brain records a diagnostic and persists the accepted base fact without malformed temporal fields.

### 2. Universal Deterministic Clocks

Every persisted fact revision has:

- `observed_at`: trustworthy source-native assertion/publication time, nullable only when the source provides none;
- `created_at`: when Brain persisted the exact revision;
- `knowledge_to`: when Brain stopped accepting that exact revision, null while open.

Job, extraction, reconciliation, and retry time never substitute for `observed_at`. Migration 23 copy-before-write revisions preserve `[created_at, knowledge_to)` knowledge intervals while retaining a stable open fact ID.

### 3. Optional Predicate Validity

Migration 22 fields remain available for claims whose truth itself is time-bounded:

- `valid_from`, `valid_to`;
- `valid_time_precision`;
- `temporal_expression`, `temporal_confidence`;
- compatibility `temporal_kind` and `effective_at`.

They are omitted for ordinary facts rather than forced to `atemporal` or `unknown`. When present, bounds are evidence-grounded ISO values and bounded intervals use `[valid_from, valid_to)`.

### 4. One Event-Centered Time

Events remain ordinary `entities` rows with `entity_type='event'`. A fact whose exactly one primary entity is an event may propose:

```json
{
  "event_time": {
    "kind": "actual",
    "start_at": "2026-07-15T09:00:00-07:00",
    "end_at": "2026-07-15T09:45:00-07:00",
    "precision": "exact",
    "expression": "July 15 from 9:00 to 9:45 AM PT"
  }
}
```

Rules:

- `kind` is `actual|planned`;
- `actual` requires `start_at` and may add `end_at`; `planned` requires at least one bound and both are ordered when present;
- `precision` is `exact|day|month|year`;
- exact timestamp bounds canonicalize to UTC for identity and storage; partial dates retain source precision;
- optional `expression`, when present, is literal source evidence for the normalized value;
- one atomic fact has at most one `event_time`;
- a missing, non-event, or ambiguous primary entity invalidates only `event_time`.

`actual` covers occurrence. `planned` covers a schedule, deadline (end only), not-before boundary (start only), or planned window (both). Multiple schedules and plan-to-actual changes are separate source-backed facts/revisions. Relative constraints between events remain ordinary facts in this release; a relative calendar expression anchored to one trusted Gmail message clock may be discovered but is review-only. Structured meeting bounds default to `planned`; trusted source-artifact update at or after the start is required to project `actual`. Capture and ingestion timestamps do not prove occurrence.

Migration 24 flattens the object into nullable `event_time_kind`, `event_start_at`, `event_end_at`, `event_time_precision`, and `event_time_expression` columns. Flattening is a storage detail; extractor and service payloads use the nested shape.

### 5. Retrieval Parity

When `temporal_mode`, `valid_as_of`, `known_as_of`, and `event_as_of` are omitted, retrieval preserves original Brain status eligibility, ranking, and packet budgets. Facts with no temporal enrichment and facts whose temporal enrichment was rejected remain eligible.

Explicit modes remain additive:

- `current`: world-valid now while retaining active facts with no predicate-valid interval;
- `valid`: explicit proposition-valid time;
- `known`: explicit Brain knowledge time;
- `bitemporal`: both explicit clocks;
- `timeline`: lineage-deduplicated temporal ordering.

`event_as_of` applies only to facts with one primary event entity; optional `event_kind=actual|planned` separates occurrences from schedules. Event filtering is orthogonal and combines with explicit validity and knowledge predicates using AND. Event time is never reused as predicate validity, observation time, or knowledge time.

## Baseline, Compatibility, And Rollback

The rollback code point is lightweight tag `temporal-cognition-baseline-2026-07-15`, commit `d5405b9`. Preserve it while this plan is active.

Rollback has two independent parts:

1. **Code rollback:** restore a build from the baseline tag.
2. **Data rollback:** restore a verified pre-migration SQLite backup or use tested action inverses for post-migration semantic changes.

Schema work is additive and idempotent:

- migration 22 adds optional predicate-valid and knowledge-time fields without inferring values for legacy rows;
- migration 23 adds revision lineages and one-open-revision constraints;
- migration 24 adds nullable event-time columns;
- legacy rows and old callers retain their behavior;
- a live home is never migrated or backfilled without a verified recoverable backup.

## Delivery Sequence

### T0 — Contract And Baseline

Deliverables:

- update the owning specs to the parity-first architecture;
- normalize all new extraction behavior to extractor v12;
- retain and verify the rollback tag.

Gate: the docs distinguish base facts, predicate validity, event time, observation time, and knowledge time, and state the fail-open/default-parity rules unambiguously.

### T1 — Migration 24 And Exact Serialization

Deliverables:

- add the five nullable flattened event-time columns and indexes needed by bounded filtering;
- serialize the nested API shape through persistence, revisions, action inverses, exports, and service results;
- preserve migration-22/23 behavior and old-call defaults.

Tests:

- fresh and representative upgraded databases reach the same schema;
- migration reruns are no-ops;
- legacy rows remain queryable unchanged;
- copy-before-write and action apply/revert round-trip every event-time field exactly.

### T2 — Parity-First Extractor v12

Deliverables:

- keep base-fact schema requirements unchanged from original Brain;
- make predicate validity and `event_time` optional;
- validate each enrichment independently after base-fact acceptance;
- drop and diagnose malformed enrichment without rejecting the fact;
- derive `observed_at` only from trustworthy source-native metadata;
- deterministically project structured meeting metadata into a primary event entity, retaining `planned` unless trusted source-artifact activity at or after start supports the projector's `actual` classification;
- distinguish same-title same-day occurrences by canonical start and coalesce exact same-kind/interval batch duplicates with unioned provenance;
- revisit older watermarks once under v12, keep structured projection success from masking failed semantic extraction, and allow an explicit isolated rebuild.

Tests and fixtures cover:

- ordinary undated facts with no temporal fields;
- explicit proposition-valid intervals;
- actual and planned event times with start-only, end-only, and bounded shapes;
- missing/ambiguous primary event entity;
- invalid enum, ordering, precision, expression grounding, and relative phrases;
- preservation of the base fact in every temporal-negative case;
- structured meeting projection with source timezone preservation;
- false-empty recovery and partial-success watermarking.

T2 gate: base-fact acceptance on the original regression corpus is no lower than baseline, and all temporal false positives fail closed without reducing fact count.

### T3 — Lifecycle And Relation Safety

Deliverables:

- preserve equal assertions with different explicit validity or event-time signatures where they represent distinct states/plans;
- distinguish planned and actual event facts;
- classify a later explicitly non-overlapping state as `updates` only when entity, state slot, evidence, and intervals are sufficient;
- preserve correction/retraction and natural progression as distinct reversible actions;
- avoid automatic open-interval closure without deterministic state-slot identity.

Gate: relation recall and false-conflict results remain at least as strong as original Brain, and exact action inverses restore prior rows and links.

### T4 — Explicit Temporal Retrieval

Deliverables:

- preserve the unclocked baseline retrieval path;
- support explicit `current`, `valid`, `known`, `bitemporal`, and `timeline` modes;
- filter before final scoring without allowing ineligible rows to starve the candidate pool;
- prevent future evidence leakage in knowledge-time packets;
- expose temporal match/rejection diagnostics without adding a missing-time penalty.

Gate: original non-temporal retrieval fixtures remain steady, temporal negative controls pass, and explicit historical fixtures satisfy provenance and no-future-leakage expectations.

### T5 — Isolated Full-Corpus Rebuild And Evaluation

Deliverables:

- copy the complete live source corpus into an isolated Brain v2 home without copying the live database, indexes, runtime state, or generated workflow traffic;
- ingest and run the v12 pipeline with external Codex providers only;
- keep configured source-type policy visible in the report, including sources intentionally stored but not fact-extracted;
- do not inspect SQLite while a long rebuild transaction is active;
- after completion, compare source/document/window/fact/entity/event counts, extraction yield, rejection reasons, routing, retrieval, and a stratified fact sample against original Brain;
- use `gpt-5.6-luna` at medium reasoning for the pre-apply critic and `gpt-5.6-sol` at medium reasoning for the final sampled auditor/end judge.

T5 gate: no live-home mutation, no material original-capability regression, no unexplained fact-yield collapse, and event examples show correct entity type and event-time shape.

T5 outcome: complete in the isolated `/Users/Peter/brain-v2` runtime. The final audit snapshot has 1,378 active facts (1,313 Luna semantic facts and 65 structured-event facts), all 19 previously collapsed substantive documents recovered, and zero active timed-fact integrity violations. Sol medium sampled 75 actions, identified two weak named-entity attributions that were reverted, and passed a non-overlapping 25-action post-fix sample. This demonstrates recovered single-model coverage and structural temporal safety; it does not authorize live promotion or establish broad free-text temporal recall.

### T5A — Evidence-First Gmail Discovery And Historical Replay

The full Gmail import is the primary discovery and lifecycle evaluation surface: approximately 6,960 current threads plus every retained immutable revision. This replaces a requirement to wait weeks merely to accumulate coverage, but historical heuristics remain proxy labels rather than human truth.

Deliverables:

- discover temporal expressions deterministically per trusted Gmail message, with exact half-open evidence offsets and no page routing or fact persistence;
- normalize explicit dates/ranges first, record `resolution_basis` for inferred-year, relative, and timezone decisions, and keep lower-confidence classes review-only;
- parse structured Calendar/ICS evidence before prose once that attachment projection is available;
- require an evidence-bound integrity checksum before Gmail event time can route, then independently reload cited chunks and reproduce the clock, primary event, and complete stabilizer audit at apply time;
- replay both latest-per-thread state and historical revisions, reporting only aggregate funnels, lifecycle cues, proxy coverage, opaque sample hashes, and content-safety status;
- expose configurable historical gates and clearly distinguish measured gold metrics, classifier-derived proxies, and unavailable metrics.

Historical replay relaxes evaluation volume and waiting-period gates, not safety invariants. Initial calibration may use a 100-item blinded stratified sample rather than 500 manually labeled threads, and a successful full-history replay may reduce the live shadow requirement to a bounded 72-hour incremental canary. Wrong-occurrence routing, private-content leakage, unintended writes, and nondeterministic replay remain zero-tolerance failures.

T5A outcome: the read-only evaluator validated all 42,533 projection files, collapsed 28,306 renderer/classifier variants, and replayed 14,227 unique source revisions across 7,125 opaque thread lineages with zero detector nondeterminism or aggregate-output privacy violations. Current selection exactly matched the isolated runtime: 6,960 active threads, 165 deleted threads, and 356 fact-eligible threads (5.11%, only 0.31 percentage points above the original 4.8% benchmark). The current direct association grammar produced 96 candidates across 77 threads, but only two of 265 classifier-marked important-temporal threads and none of 288 same-message explicit-date proxy threads. All 263 important-temporal misses still contained a temporal-form proxy and temporal cue, demonstrating an association-recall gap rather than absent source time. This is a proxy diagnosis, not labeled recall.

Historical lifecycle depth was weaker than expected. After semantic projection variants were collapsed, all 7,102 adjacent source-revision transitions had identical retained source evidence. The import is therefore a strong cross-sectional syntax, filtering, privacy, and determinism surface, but it cannot validate cancellation/reschedule ordering or incremental freshness. The bounded live canary remains required.

### T5B — Review-Only Temporal Association Recall

Deliverables:

- inventory temporal expressions independently of source classification, including exact spans, normalized options, ambiguity, and resolution basis;
- inventory event, deadline/action, boundary, lifecycle, and artifact mentions independently of dates, then retain bounded association hints with explicit modes and risk features; hints rank review work but never define the selector's recall boundary;
- partition every recognized expression into an exact, segment-local selector packet with a hard byte/endpoint ceiling, optional subject-line event bridge, and explicit coverage-or-omission accounting;
- use `important_temporal` only to prioritize an unresolved review lane, never to invent a relation, entity, or date pairing and never as self-validating recall evidence;
- allow a separately identified temporal-rescue lane to inspect source-suppressed mail, while preserving `fact_admitted=False` and forcing every rescued positive association to low-confidence deferral;
- admit a review-only singleton association only within one trusted message with exactly one supported expression and one event/action mention, no lifecycle cue, and no competing date or event;
- keep arrival, ending, completion, cancellation, and reschedule semantics out of occurrence-start auto-application; a parent event's terminal boundary is not its start;
- project and parse Calendar/ICS bodies with UID, sequence, status, DTSTART/DTEND, and timezone before making structured schedules auto-eligible;
- create a blinded 100-120 item stratified calibration set spanning direct hits, important-temporal misses, explicit-proxy misses, human-mail leads, lifecycle language, and bulk/advertising negatives;
- freeze the HMAC-opaque cohort before labeling, require complete labels for every selected record, and treat sparse, selectively omitted, duplicate, or stale cohort membership as failed or not evaluated rather than extrapolating from favorable annotations;
- keep every new association sidecar-only until class-specific calibration succeeds; temporal misses must never suppress the accepted base fact.

The implemented selector deliberately has less authority than the earlier design. It may decide materiality and cite only an inventoried temporal expression, an event/action/title subject, an optional lifecycle cue, and an optional matching hint. It cannot author relation, planned/actual kind, lifecycle, normalization, confidence, source text, spans, dates, or explanations. A deterministic planner presents one expression in a local source segment, a bounded mention subset, an optional subject bridge, and a small number of hints; every packet has a source- and analysis-bound authority manifest. Deterministic validation derives all semantics, rejects stale or cross-packet evidence, and converts incomplete, risky, or conflicting assertions to `defer_ambiguous`. Each association is validated independently before deterministic deduplication and reduction so one bad citation cannot erase valid siblings. Endpoint IDs are content-bound, terminal boundaries can never become occurrence starts, and temporal-rescue output remains non-routable.

Review-only output may preserve source-bound alternatives and reschedule endpoints as separate deferred sidecars. That is acceptable while a human is the final interpreter: both cited endpoints remain visible, unresolved semantics are explicit, and neither sidecar may persist state or trigger action. Before temporal output can be persisted as an operative schedule, routed into a workflow, or used for reminders, the representation must group the related endpoints and encode their order and role explicitly—for example `alternative`, `rescheduled_old`, and `rescheduled_replacement`. Source order or two nearby dates alone is not sufficient authority for automation.

The first 120-record stratified cohort is now a development set because it was reused while the analyzer, graph, prompt, and selector were changing. It is useful for architecture comparison, not for satisfying the human-grounded promotion gate. The winning development architecture, its aggregate results, and the required fresh holdout are recorded in [Gmail Temporal Recall Exploration](../audits/gmail-temporal-recall-exploration-2026-07-20.md).

T5B development outcome: the endpoint-only arm preserved a 68.7% Sol-supported material-record rate while raising the Sol-supported proposal rate from 63.4% to 80.0% and reducing critical-error labels from 23 to 15. These are independent-model support rates on reused development data, not human-grounded recall or precision. A diagnostic remap through the stricter validator then produced a 62.4% supported material-record rate, an 80.6% supported proposal rate, and 14 critical-error labels; it removed eight proposals but exposed the coverage cost of fail-closed repair. A later boundary-only diagnostic remained below target at 53 of 71 useful records and 63 of 80 supported proposals, so validator relaxation was rejected as the primary recall strategy. The expression-centric packet planner, explicit temporal rescue, structured event-title candidates, segment-local lifecycle guards, and association-isolated reducer are now implemented and covered by the full regression suite. Their fresh external Gmail quality pass awaits explicit informed approval to transmit the private cohort; see [Gmail Temporal Recall Iteration](../audits/gmail-temporal-recall-iteration-2026-07-21.md). No free-text class can emit high confidence or route automatically. Structured ICS extraction, stable lifecycle identity with a separate reconciliation pass, a fresh human holdout, and a live content-changing canary remain required.

### T6 — Controlled Promotion And Optional Backfill

Deliverables:

- run fresh-home and representative schema-21 upgrade acceptance;
- capture and verify a live backup plus recovery steps;
- migrate a bounded shadow copy and compare current answers before promotion;
- expose any historical temporal enrichment as a separate dry-run report.

Historical backfill is optional and never implicit. Unknown/absent time is a valid outcome; coverage is not purchased with invented precision.

## Promotion Gates

Hard gates remain mandatory:

- migrations 22-24 are additive, idempotent, and fixture-upgrade tested;
- extractor v12 preserves base-fact recall and source provenance;
- malformed temporal enrichment never rejects a valid base fact;
- event time requires exactly one primary event entity and survives revisions/inverses;
- observation, knowledge, predicate-valid, and event clocks are never conflated;
- default retrieval remains compatible and explicit historical modes prevent future leakage;
- natural updates and corrections retain distinct reversible semantics;
- existing extraction, relation, retrieval, action-inverse, daemon, and architecture-boundary suites pass;
- the isolated full-corpus report explains yield and rejection differences;
- live migration/backfill remains blocked until backup and recovery verification succeeds.

Historical evidence gates are deliberately pragmatic and configurable. They reduce waiting and annotation volume, but proxy breadth never substitutes for correctness:

- the evaluator processes the complete current projection and retained history without emitting private fields or raw expressions;
- independent direct-association coverage over classifier-marked important-temporal current threads continues to be reported against the earlier 85% review-queue target, explicitly labeled as a proxy rather than recall; classifier-assisted leads are reported separately and cannot satisfy this target by construction;
- the earlier blinded-cohort gate required at least 100 frozen records, at least five records in each required deterministic stratum, complete annotation, at least 85% human-labeled temporal recall, at least 95% supported-time precision, and zero critical occurrence, timezone, cancellation, or reschedule errors. That 85% recall threshold remains a historical development floor for comparison; it does not satisfy the current personal-use release bar below;
- the independent `gpt-5.6-sol` medium audit accepts at least 95% of the review-eligible sample with zero critical errors rather than requiring every stylistic judgment to pass;
- the previous proxy comparison also kept base-fact acceptance and default retrieval within two percentage points of the original Brain benchmark; temporal failure never removes a valid base fact, and advertising produces zero auto-applied facts;
- deterministic replay across retained revisions produces no duplicate active occurrence and preserves cancellation/reschedule order;
- after cross-sectional historical gates pass, a 72-hour incremental shadow can replace the earlier two-week waiting period before a review-only canary; the canary must still exercise real content-changing revisions because this historical projection did not;
- inferred-year, relative, unzoned, abbreviation-based, cross-span, multi-event, lifecycle, and classifier-assisted associations remain review-only in this release.

The current personal-use, review-only release bar supersedes the earlier 85% development floor. It is evaluated on a newly frozen, fully labeled holdout after alias collapse, with required members of alternatives and reschedules retained in the same semantic unit:

- at least 95% effective temporal recall, counting a correctly supported citation or a correctly deferred uncertainty sidecar as recovered, with no incomplete required-member unit hidden by aggregate member counts;
- at least 90% confirmed temporal recall, counting supported citations only;
- at least 95% confirmed precision among supported temporal artifacts;
- at least 95% retention of the original Brain's non-temporal facts and capabilities on a frozen parity cohort, with temporal failure never removing a valid base fact;
- the relevant result appears in the top five for at least 90% and in the top ten for at least 95% of the frozen temporal-retrieval queries;
- zero critical errors, including wrong occurrence, subject, lifecycle direction, endpoint role, timezone, or unsupported promotion;
- at least 95% semantic/cluster stability across three separately invoked runs, measured after alias collapse rather than by raw candidate IDs; and
- advertising and other suppressed noise produce zero auto-applied facts.

These thresholds authorize review-only use, not silent automation. Automatic persistence, routing, reminders, or other action remains limited to separately calibrated direct or structured classes after grouped and ordered alternative/reschedule roles are implemented. A silent class must show 98–99% observed precision—98% as the minimum for low-impact personal automation and 99% for state-changing or externally visible action—with zero critical errors on a fresh class-specific holdout.

## Cleanup Policy

Temporal implementation does not authorize legacy deletion. Any cleanup must be a separately reviewed tranche with proof of no runtime, migration, fixture, export, or rollback reachability and representative fresh-install/upgrade tests.
