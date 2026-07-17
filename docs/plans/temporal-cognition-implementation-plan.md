# Temporal Cognition Implementation Plan

**Status:** T0-T5 implemented and evaluated in isolated Brain v2; live migration, optional backfill, and controlled promotion remain open
**Last verified:** 2026-07-17 against the unpromoted temporal-cognition working tree based on baseline commit `d5405b9`; the isolated full-corpus rebuild, targeted repair, final Sol-medium audit, wiki/index refresh, and 963-test suite are complete
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
- No processing-time fallback for `observed_at`, guessed historical dates, or implicit resolution of relative phrases.
- No temporal parse failure may become a base-fact validation failure.
- No default-retrieval recall loss is acceptable.
- No destructive legacy cleanup in this tranche.
- Every semantic fact mutation remains an existing policy decision recorded in `cos_actions` with an exact inverse.

## Target Architecture

### 1. Base Fact First

Extractor v12 proposes the same durable fact as original Brain: statement, evidence-unit references, claim class, entities, route, and extraction/routing/truth confidence. Deterministic validation decides whether that base fact is admissible before considering optional annotation and temporal enrichment. Unsupported durable claim labels and malformed entity annotations fail soft so an evidence-backed base fact survives.

Temporal parsing is a subordinate step. If it fails, Brain records a diagnostic and persists the accepted base fact without the malformed temporal fields.

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

`actual` covers occurrence. `planned` covers a schedule, deadline (end only), not-before boundary (start only), or planned window (both). Multiple schedules and plan-to-actual changes are separate source-backed facts/revisions. Relative statements such as “before the summit” remain ordinary facts in this release. Structured meeting bounds default to `planned`; trusted source-artifact update at or after the start is required to project `actual`. Capture and ingestion timestamps do not prove occurrence.

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

### T6 — Controlled Promotion And Optional Backfill

Deliverables:

- run fresh-home and representative schema-21 upgrade acceptance;
- capture and verify a live backup plus recovery steps;
- migrate a bounded shadow copy and compare current answers before promotion;
- expose any historical temporal enrichment as a separate dry-run report.

Historical backfill is optional and never implicit. Unknown/absent time is a valid outcome; coverage is not purchased with invented precision.

## Promotion Gates

The feature is promotable only when:

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

## Cleanup Policy

Temporal implementation does not authorize legacy deletion. Any cleanup must be a separately reviewed tranche with proof of no runtime, migration, fixture, export, or rollback reachability and representative fresh-install/upgrade tests.
