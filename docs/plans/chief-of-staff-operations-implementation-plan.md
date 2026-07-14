# Chief-of-Staff Operations Implementation Plan

**Status:** execution started; COS-0 complete, COS-1/COS-2 in progress; the COS-2 isolated kernel slice is implemented
**Last verified:** 2026-07-13 against architecture commit `a44b713` plus the current operational-kernel working tree
**Owning spec:** [Chief-of-Staff Operations](../specs/chief-of-staff-operations.md)

## Outcome

Turn Brain into a proactive personal Chief of Staff without destabilizing the working knowledge layer.

The program keeps one product, one app, one coordinator daemon, and one Brain home. It adds a separate operational bounded context for current commitments, events, waiting-on state, attention, and briefings. Knowledge Curation remains responsible for durable facts and wiki structure. External actions remain behind a separately permissioned execution boundary.

## Fixed Decisions

1. `PKM Brain.app` remains the only desktop product and supervises the only coordinator daemon.
2. Operational state uses `db/ops.sqlite`; it does not add tables to `brain.sqlite`.
3. `ops_items` is the one current-state aggregate. Immutable observations and append-only item events preserve evidence and lifecycle history without creating per-type ontologies.
4. Provider-native identity drives reconciliation before semantic matching.
5. Read-only Calendar is the first vertical slice and requires no LLM.
6. Today is the only V1 navigation change. Queue remains Knowledge Curation review.
7. Gmail retrieval, durable-fact admission, and operational detection are separate lanes.
8. The Gmail operational detector is one-stage and high-recall. It does not call the fact critic or per-candidate route resolver.
9. The current `cos_*` symbols remain frozen legacy names for Knowledge Curation until one atomic rename tranche. Operational code never uses them.
10. No external provider write is authorized before draft-only evaluation and a payload-bound approval boundary exist.

## Program Sequence

| Phase | Outcome | External writes | State |
|---|---|---:|---|
| COS-0 | verified knowledge-layer foundation | none | complete |
| COS-1 | canonical boundary, privacy, eval, and rollout contracts | none | in progress |
| COS-2 | separate operational kernel and deterministic lifecycle | none | in progress |
| COS-3 | read-only Calendar evidence and reconciliation | none | planned |
| COS-4 | coverage-aware Today shadow briefing and feedback | none | planned |
| COS-5 | Gmail retrieval plus one-stage operational detection | none | gated |
| COS-6 | cross-source reconciliation and production briefing gates | none | gated |
| COS-7 | local draft/action plans with guarded approval protocol | none | gated |
| COS-8 | capability-by-capability external execution | explicit only | gated |

Calendar and the operational kernel precede Gmail content access. Detection must work before notifications. Reconciliation must work before the briefing is considered trustworthy. Drafting must work before execution.

## COS-0 - Knowledge Foundation Baseline

Completed by commit `3937316`:

- knowledge schema 21 and current fact/entity/wiki behavior preserved;
- auth-only Gmail and Slack connector shells;
- isolated Gmail benchmark with reconciled token accounting;
- 510 Python tests, 17 Swift tests, Ruff, and the full macOS UI acceptance path green;
- production Gmail remains disabled and identity-only.

The historical `audit-remediation-2026-07-12` branch is not merged because its semantic fixes were replayed into the newer main history.

## COS-1 - Boundary And Contracts

### Deliverables

- canonical operational spec and this plan;
- updated product, capture, curation, app, retrieval, sync, README, and compatibility pointers;
- explicit Calendar and Gmail privacy/scope/retention contracts;
- a versioned operational eval fixture format;
- an all-or-nothing Knowledge Curation rename decision.

### Naming decision

Conceptual documentation changes immediately from "Chief-of-Staff curation" to "Knowledge Curation." Physical `cos_*` modules, tables, CLI, API/config fields, migrations, fixtures, and tests remain unchanged through COS-4. After the Calendar shadow vertical slice is accepted, one dedicated tranche may rename the entire surface to `curation_*`, with compatibility aliases and a dated removal horizon. A partial rename is prohibited.

### Exit gate

Every new operational table, job, API, and UI control has exactly one owning spec. No document describes `cos_actions` as the operational action ledger.

Architecture and privacy contracts were completed by commit `a44b713`. The versioned private-eval fixture artifact remains before the COS-1 exit gate closes.

## COS-2 - Operational Kernel

### Persistence

Create an independently migrated `db/ops.sqlite` with:

- `ops_schema_migrations`;
- `ops_observations` for immutable normalized source revisions;
- `ops_items` for canonical current state;
- `ops_item_events` for append-only transitions and feedback;
- `ops_source_cursors` for replay-safe connector progress and source coverage.

Common item fields remain columns: source-unit/object identity, kind, state, title, owner/counterparty metadata, starts/due/ends/expires/snooze times, priority, confidence, current observation, reconciliation method, human-action provenance, and timestamps. Provider/type-specific material stays in validated JSON until repeated usage proves a column is necessary.

Initial item kinds are `event`, `commitment`, `waiting`, `follow_up`, `deadline`, and `attention`. The universal state enum is `active`, `resolved`, `dismissed`, `cancelled`, or `expired`. Kind-specific semantics are expressed through deterministic transition validation rather than separate tables.

### Reconciliation

The write path is:

```text
normalized source revision
  -> immutable observation (idempotent)
  -> canonical-key match
  -> deterministic transition
  -> current item projection + append-only event
```

Rules:

- identical source revision/hash is a no-op;
- a changed revision with a newer connector-supplied provider-authority order updates the same canonical item;
- older or equal-authority distinct revisions are retained as `stale_ignored` history and cannot replace the projection;
- revision strings and content hashes never determine chronology;
- changes to start/end/due time emit `rescheduled`;
- provider cancellation of an existing item emits `cancelled`; a first-seen tombstone creates a terminal item with `created` plus `source_event=cancelled` metadata;
- human `resolved|dismissed` state remains sticky while direct source cancellation is retained in the current observation/event; explicit restore reveals the source-derived `active|cancelled` state;
- user dismissal is sticky until explicit user restore or a materially new episode;
- absence from one poll never implies completion;
- ambiguous identity creates a separate `active` item with provisional/ambiguous reconciliation metadata rather than a false merge;
- no write transaction spans `brain.sqlite` and `ops.sqlite`.

### Modules

- `operational_migrations.py`
- `operational_db.py`
- `operational_state.py`
- `paths.py` for the operational DB path

### Current implementation

The first isolated kernel slice exists in the current tree:

- independently migrated `ops.sqlite` tables and an explicit bootstrap path;
- immutable bounded observations, canonical items, append-only hashed events, and replay-safe source cursors with generation compare-and-swap;
- exact source-unit binding, strict UTC normalization for present timestamps, provider-authority reconciliation, stale/equal-authority protection, lifecycle feedback, and one atomic source-unit/cursor batch primitive;
- owner-only database/WAL/SHM handling, bounded lock retry, and focused isolation/concurrency tests.

It is intentionally not called by `brain init`, the daemon, connectors, CLI, API, or UI yet. Before COS-2 is complete, the service layer must add primary/single-role writer fencing and serialized mutation ownership, and coordinated backup/restore must cover `brain.sqlite` plus `ops.sqlite`. Until then, no production operational database or behavior is enabled.

### Verification

- fresh-store and same-version re-initialization idempotence; add a populated upgrade fixture before the first post-v1 migration;
- WAL, busy timeout, foreign keys, and short transaction behavior;
- observation replay idempotence;
- update/reschedule/cancel/dismiss/restore history;
- concurrent short-writer coverage;
- a test proving no knowledge table changes.
- owner-only DB/WAL/SHM permissions and explicit missing-store failure;
- primary/single-role writer fencing at the service boundary;
- a coordinated backup/integrity fixture covering both SQLite databases.

### Exit gate

A deterministic fixture replay can create, update, reschedule, cancel, resolve, and dismiss one item without duplication or any `brain.sqlite` mutation. The daemon/service rejects secondary writes, and a coordinated backup/restore fixture preserves the item and its human feedback.

## COS-3 - Read-Only Calendar

### Connector contract

Add a separate Google Calendar account grant with identity plus `https://www.googleapis.com/auth/calendar.events.owned.readonly`. The initial application policy reads only the owned primary calendar; additional owned calendars require an explicit ID allowlist, and shared calendars require a separate decision about the broader read scope. Do not widen the Gmail credential. Credentials remain in Keychain; local config contains only non-secret account/status/cursor metadata.

Poll a bounded past/future window with deleted events included. Normalize the minimum necessary fields:

- account/calendar/event ID and revision;
- iCal UID where available;
- recurring master plus original-instance identity;
- title and privacy-safe details;
- timed/all-day values and source timezone;
- organizer, attendee role, and RSVP;
- status/cancellation and provider update time.

Stage each complete Calendar change-set before reconciliation. Map the event etag to the opaque revision when present, the committed per-calendar sync generation to `source_order`, Calendar `updated` to optional `source_updated_at`, and Calendar `sequence` to bounded metadata. For an ID-only tombstone, derive an idempotence-only revision from the sync checkpoint plus minimal identity and leave provider update time absent. Commit observations and the next sync token/generation in one source-unit transaction; never use page position, lexical etag order, or poll time as authority.

### Deterministic identity

- ordinary event: account + calendar + event ID;
- recurring instance: recurring event ID + original start time;
- mirrored-calendar duplicates: retain both observations, suppress only in briefing projection using iCal UID + start time until stronger evidence exists.

### Failure behavior

- pagination, rate limit, auth refresh, and transient failure are bounded and retryable;
- a failed poll does not advance the source cursor;
- partial coverage is recorded and surfaced;
- private events retain minimal provider-safe representation;
- declined/cancelled events do not become active attention by default.

### Exit gate

An opted-in Calendar shadow run produces no LLM calls and no external mutation, while recurrence exceptions, moves, cancellations, timezones, and replay pass labeled fixtures.

## COS-4 - Today Shadow Briefing

Extend `/api/digest` with an optional briefing projection and add a narrow feedback endpoint. Do not add a navigation destination.

Today renders:

1. source coverage/freshness;
2. now and today;
3. upcoming and changed;
4. needs correction or uncertain;
5. the existing system pulse below the briefing.

Every item supports evidence inspection plus correct, done, snooze, dismiss, restore, and report-missing feedback where valid. Feedback appends an operational item event; it does not masquerade as a provider observation and never mutates a fact merely because the user changes an item.

### Briefing requirements

- deterministic ranking with injected clock/timezone;
- no cancelled/resolved/dismissed item in active sections;
- no duplicate mirrored event in the top projection;
- explicit `Calendar only`, `Gmail unavailable`, `stale`, and `incomplete` coverage labels;
- persisted briefing runs only when needed for shown/not-shown evaluation and feedback attribution.

### Exit gate

A user can inspect and correct a Calendar-backed briefing for at least two weeks of shadow replay without duplicate or stale cards exceeding the configured gates.

## COS-5 - Gmail Retrieval And Operational Detection

This phase requires explicit approval of Gmail read-only scope, local retention, redaction, deletion, attachment, and quoted-history behavior.

### Three independent lanes

1. Retrieval indexing: approved normalized thread snapshots.
2. Durable knowledge: the conservative human/evidence fact pipeline.
3. Operational detection: high-recall provisional current-state operations.

One changed thread is processed once, or in a failure-isolated batch of small transactional threads. Input contains new messages, bounded thread context, source-native timestamps, and compact plausibly related active items. Output is a structured operation:

- ignore;
- create item;
- update/reschedule/cancel/close item;
- needs reconciliation.

No fact critic, fact route resolver, entity gardener, or wiki routing is invoked. A malformed response is retried only as bounded schema repair. Cost is measured rather than inferred; 100–150K tokens/day is a planning hypothesis, not a benchmark result.

### Label program

Shadow predictions are not labels. Build a chronological, versioned set containing:

- surfaced positives;
- stratified suppressed mail;
- full-day audits;
- missed-item reports;
- immutable holdout days;
- operational importance, type, state, evidence, due/owner, and sensitivity labels.

### Exit gate

Severity-weighted recall, false-alarm rate, source-date accuracy, schema-repair rate, and token/call budgets meet their approved thresholds on held-out replay.

## COS-6 - Cross-Source Reconciliation

Link Calendar and Gmail only after source-local identity is reliable.

Deterministic candidate keys precede semantic candidates:

1. provider object/thread/message lineage;
2. explicit business identifier or link;
3. existing item/observation lineage;
4. participants + normalized subject + temporal proximity;
5. bounded semantic similarity as a candidate generator only.

Ambiguous matches remain separate or require confirmation. False merge is treated as more severe than a temporary duplicate.

### Required metrics

- duplicate-active and stale-active rate;
- false-merge and false-split rate;
- update/reschedule/cancel/closure recall;
- premature-close rate;
- resolved-item resurrection;
- wrong person/project/episode linkage;
- high-severity miss rate and detection latency;
- replay idempotence;
- briefing precision@K and stale/duplicate share.

### Exit gate

The production briefing remains disabled until chronological replay meets the reconciliation gates, not merely detection precision.

## COS-7 - Draft-Only Actions

Add a local outbox only after read-only trust gates pass. An action plan binds:

- account/tenant, capability, adapter version, and target IDs;
- exact payload and human-readable preview;
- before-state hash/precondition;
- verification plan;
- compensation plan when one exists;
- reversibility class, risk, expiry, and idempotency key.

The capability registry declares `read_only`, `reversible`, `compensable`, or `irreversible`. Calendar changes default to compensable. Draft email creation may be reversible. Sent email is irreversible.

The agent/planner cannot approve through the same API surface. COS-7 creates plans and previews only; it requests no provider write scope.

## COS-8 - Capability-Gated Execution

Reuse the Monarch Guard protocol invariants:

```text
plan without mutation
  -> payload-bound human approval
  -> before-state drift check
  -> commit
  -> verify + provider receipt
  -> hash-chained audit
  -> rollback, compensation, or external reconciliation
```

Enable one capability at a time. Each capability needs live sandbox fixtures, failure recovery, duplicate-delivery protection, and a separately approved autonomy policy. Irreversible actions require explicit human approval and remain non-autonomous initially.

## Evaluation And Release Gates

Every shadow release records:

- source coverage and replay window;
- calls, total/uncached tokens, latency, and invalid-output rate;
- item precision/recall by kind and severity;
- reconciliation and briefing metrics;
- corrections, dismissals, snoozes, and reported misses;
- DB size, write latency, lock errors, backup/integrity result;
- model/prompt/classifier versions.

No release can average away a wrong-person link, false closure of a user-confirmed commitment, missed high-consequence cancellation, or unapproved external write.

## Backup, Sync, And Recovery

- `ops.sqlite` uses SQLite backup semantics, never a live file copy.
- Backup manifests bind the knowledge and operational snapshots taken under one daemon-controlled recovery point.
- Operational state is primary-only initially and is never live-rsynced.
- A secondary can render explicitly stale replicated briefing state only after coordinated snapshot support lands.
- Immutable provider evidence allows deterministic rebuild of derived observations/items, but user corrections and approvals are canonical and must be backed up.
- Fact promotion is an asynchronous replay-safe handoff into the normal Knowledge Curation action path.

## Stop Conditions

Stop the phase and preserve the previous release when:

- an operational write touches `brain.sqlite` directly;
- a knowledge action is used to represent a current item or external side effect;
- Calendar recurrence/timezone/cancellation replay is not deterministic;
- source coverage is incomplete but Today presents an all-clear;
- duplicate, stale, false-merge, premature-close, or high-severity-miss gates regress;
- calls/tokens grow without a per-changed-source budget;
- SQLite lock/integrity or coordinated-backup tests fail;
- an external plan lacks exact approval binding or a reversibility class;
- planner and approver share the same authority surface.

## Commit And Rollout Discipline

Use separate commits for:

1. knowledge foundation;
2. operational specs/plan;
3. operational DB/kernel;
4. Calendar source;
5. Today/API/UI;
6. Gmail retrieval/detection;
7. cross-source reconciliation;
8. draft/execution capabilities.

Every implementation commit updates the owning phase status and includes focused tests plus the full no-regression gate appropriate to its blast radius.
