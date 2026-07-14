# Chief Of Staff Operations

**Status:** canonical feature spec; implementation is in progress and no operational behavior is released
**Last verified:** 2026-07-13 against knowledge-foundation commit `3937316e090a`; requirements below govern the in-progress implementation
**Owns:** operational items, reconciliation, briefings, operational evaluation, and guarded external execution

## Mission And Product Boundary

PKM Brain's product mission is to act proactively as a local Chief of Staff for one operator. The existing knowledge system is the evidence, identity, retrieval, and policy foundation for that mission; it is not itself the operational Chief of Staff.

The product remains one application, one supervised daemon, and one Brain home. Chief-of-Staff operations are a separate bounded context inside that product, not a second app or service:

```text
private sources
  -> common evidence substrate
       -> Knowledge Curation -> facts/entities/wiki/retrieval
       -> Chief Of Staff Operations -> current work state -> Today briefing
       -> Guarded Execution -> approved external effects
```

Knowledge Curation and Chief Of Staff Operations have different lifecycles:

- facts maintain durable, source-backed truth;
- operational items track changing obligations, schedules, waiting, and attention;
- briefings are derived views of current operational state;
- external actions require a separate approval and audit boundary.

Operational items MUST NOT be stored as facts merely to reuse the knowledge pipeline. Facts MUST NOT be silently converted into current work. Promotion in either direction requires an explicit, source-backed operation with its own provenance.

The initial release is read-only and shadow-only. No Calendar or Gmail mutation scope is authorized by this spec.

## V1 Scope

V1 proves one closed loop:

1. capture read-only Calendar evidence;
2. reconcile it into current operational state;
3. generate an evidence-backed Today briefing;
4. accept local corrections and completion/dismissal feedback;
5. measure duplicate, stale, missed, and false-alarm rates;
6. add read-only Gmail operational detection only after the Calendar loop passes.

V1 keeps the existing seven-destination navigation. Today becomes the briefing surface. Queue remains the Knowledge Curation review workflow, and Ops remains the daemon/system-administration destination.

Goals are operator-authored intent, not extracted observations. V1 MAY read an explicitly configured human-authored Markdown page, defaulting to `wiki/goals.md`, as ranking context. It MUST NOT create a goal table or infer goals from mail.

## Authority And Lifecycle

Operational authority is ordered as follows:

1. Normalized source artifacts and provider identifiers are evidence.
2. Explicit operator corrections, confirmations, dismissals, and completions are authoritative operational decisions.
3. `ops_items` is the canonical current operational read model.
4. `ops_observations` preserves immutable normalized source revisions.
5. `ops_item_events` is the append-only history explaining how current item state changed.
6. Briefings, rankings, summaries, model rationales, and eval reports are derived artifacts.

A model suggestion is never evidence by itself. A source-backed detector may propose a transition, but deterministic code validates and applies it. Absence of a new message, model timeout, malformed output, or low confidence MUST NOT complete, cancel, or merge an existing item.

Human decisions outrank later automated inference:

- a dismissed item is not reactivated from the same source version;
- a corrected date or owner remains until newer direct evidence or another human correction changes it;
- a human-resolved item may reactivate only from materially new evidence or an explicit human restore, with that reason recorded in the transition event;
- every override records the source version and prior state it superseded.

## Operational Item Model

V1 has one business-object table, `ops_items`. Item kinds are an enum on that table rather than independent schemas or state machines.

### Item Kinds

| Kind | Meaning |
|---|---|
| `event` | a scheduled occurrence, normally sourced from Calendar |
| `commitment` | an action the operator has agreed to complete |
| `waiting` | a result, response, or deliverable currently owed by another party |
| `follow_up` | an operator-owned reminder to check, reply, or revisit after an earlier interaction |
| `deadline` | a time-bound external condition such as a payment, renewal, reservation, or filing |
| `attention` | a decision, risk, ambiguity, or change that needs deliberate review |

Goals are not an item kind. The six kinds share one lifecycle and one table; they do not create separate ontologies.

### Item States

The universal state enum is:

- `active`: current, including low-confidence or reconciliation-ambiguous work;
- `resolved`: completed, satisfied, or deterministically ended;
- `dismissed`: explicitly marked irrelevant or incorrect by the operator;
- `cancelled`: directly cancelled by the source or operator;
- `expired`: its declared attention window elapsed without implying completion.

Provisional or ambiguous status is not a lifecycle state. It is represented by `confidence`, `reconciliation_method`, and validated metadata such as `reconciliation_status=confirmed|provisional|ambiguous`. Briefing admission and presentation use those fields without inventing another item state.

Allowed transitions are:

```text
active    -> resolved | dismissed | cancelled | expired
resolved  -> active    only from materially newer evidence or human restore
expired   -> active    only from materially newer evidence or human restore
dismissed -> active    only through explicit human restore or a new episode
cancelled -> active    only when the provider restores/reschedules it
```

Passing a commitment deadline marks the active item overdue; it does not resolve it. Calendar events may transition to `resolved` after their authoritative end time. `expired` is reserved for signals with an explicit expiry rule.

### Required `ops_items` Fields

| Field | Contract |
|---|---|
| `id` | stable local ID |
| `canonical_key` | deterministic identity key, unique within account/provider scope |
| `item_kind` | closed enum above |
| `state` | closed enum above |
| `title` | bounded operator-facing summary |
| `details` | optional bounded explanation; never a full source payload |
| `owner` | `operator|other|shared|unknown` |
| `account_key` | local non-secret connector-account identity |
| `counterparty_entity_id` | optional stable Brain entity reference; no cross-database foreign key |
| `project_ref` | optional page/entity reference used for ranking |
| `starts_at`, `ends_at` | optional source-native event interval in UTC |
| `due_at` | optional obligation deadline in UTC |
| `source_timezone` | source timezone needed to render or reconstruct local dates |
| `expires_at` | optional attention expiry; never a completion time |
| `snoozed_until` | optional local briefing suppression without changing truth/state |
| `priority` | bounded numeric rank with deterministic source/rule provenance |
| `confidence` | calibrated `0.0..1.0` detection/reconciliation confidence |
| `current_observation_id` | current immutable `ops_observations` row |
| `reconciliation_method` | exact-provider, deterministic, semantic, or human method/version |
| `metadata` | validated owner/counterparty, evidence, reconciliation-status, source-version, snooze, and resolution detail |
| `created_at`, `updated_at` | lifecycle timestamps |

Dates are optional. The system MUST NOT invent a due date because an email says “soon” or because a model expects one. Unknown owner, date, or project remains explicit.

### Event History

`ops_observations` stores immutable normalized source revisions. Each row records source type, account key, source key and revision, observation time, proposed item kind, `upsert|cancelled` event kind, validated payload, content hash, bounded evidence references, and creation time. Replaying the same source revision is a no-op.

`ops_item_events` is an append-only technical history, not a second domain model. Each event records:

- event ID and item ID;
- `created|updated|rescheduled|cancelled|resolved|expired|feedback` event type;
- actor class `connector|deterministic|model|human`;
- source reference and source version where applicable;
- prior and resulting state;
- run ID and reconciliation version;
- bounded structured transition detail and its hash;
- event timestamp.

`ops_source_cursors` stores replay-safe connector/account/stream cursors, watermarks, metadata, and last-success time. A cursor advances only after every observation/item/event write for that source unit commits.

The current item update and its event append MUST commit in one `ops.sqlite` transaction. Events and observations are immutable. Corrections append feedback/transition events rather than rewriting history.

## Storage And Concurrency

Chief-of-Staff state lives in `~/brain/db/ops.sqlite`, separate from the knowledge control plane. This is one product and daemon with two physical SQLite databases.

The separation is intentional:

- operational polling creates frequent small writes;
- Knowledge Curation may perform long fact/critic/rebuild tranches;
- operational items have different retention and recovery behavior;
- a briefing must not compete with knowledge writes or cause knowledge lock failures.

Storage requirements:

- the physical tables are `ops_schema_migrations`, `ops_observations`, `ops_items`, `ops_item_events`, and `ops_source_cursors`;
- only the Python daemon/service layer opens `ops.sqlite`; Swift and browser clients never do;
- the file, WAL, and SHM use owner-only permissions;
- schema migrations are independently versioned and idempotent;
- foreign keys within `ops.sqlite` are enabled;
- evidence/entity/page references into the knowledge DB use stable IDs and are resolved through services, not cross-database foreign keys;
- write transactions MUST NOT use SQLite `ATTACH` or require atomic commits across both databases;
- cross-plane reads are bounded and performed outside an operational write transaction;
- item/event writes retry bounded transient locks and fail visibly after the retry budget;
- the daemon's mutation executor prevents overlapping operational writes.

Briefing snapshots are derived. They MAY be cached in `ops.sqlite` with generation time, horizon, ordered item IDs, ranking reasons, completeness, and connector coverage, but MUST NOT duplicate source bodies. Cached briefing snapshots expire after 30 days by default.

### Backup And Recovery

A recovery snapshot that claims complete Brain state MUST include independent SQLite-backup-API snapshots of both knowledge and operational databases plus a manifest containing schema versions, generation time, and hashes. Copying live DB/WAL files is invalid.

If `ops.sqlite` is unavailable:

- knowledge ingest, retrieval, and curation continue;
- Today reports operational state unavailable rather than showing stale data as current;
- the system does not silently create an empty replacement over a damaged database;
- restore or explicit rebuild is required;
- rebuilding from source may recover detections, but human corrections and dismissals require a backup.

Primary/Secondary V1 keeps canonical operational writes on the primary. `ops.sqlite` is never live-rsynced. Secondary operational read/write and coordinated promotion remain blocked until the sync spec defines snapshot and epoch behavior for both databases.

## Reconciliation Contract

Detection finds possible work. Reconciliation maintains one trustworthy item across updates, replies, moves, cancellations, and completion. It is the central correctness boundary.

Every changed source follows this order:

1. normalize source/provider identity and source version;
2. ignore an already-applied source version idempotently;
3. match an exact provider/thread lineage key;
4. apply deterministic update, cancellation, or completion rules;
5. compare only a bounded set of compatible active items for cross-source linkage;
6. use one semantic detector result when deterministic evidence is insufficient;
7. create a separate active item marked low-confidence/ambiguous rather than force an ambiguous merge;
8. write the item transition and append-only event atomically.

Deterministic identity takes precedence:

- Calendar keys include account, calendar, event ID, and recurring-instance identity/original start when applicable;
- Gmail keys include account and thread ID, plus a stable detector-local item key when one thread contains multiple obligations;
- message IDs and reply/reference lineage identify new observations inside a thread;
- provider update versions and normalized hashes make replays idempotent.

Cross-thread matching may consider compatible kind, owner/counterparty, entity/project, normalized subject/action, business identifiers, and temporal proximity. It MUST NOT merge across connector accounts solely because text is similar.

Reconciliation rules:

- direct cancellation/reschedule evidence may update the matching event;
- a changed due date supersedes the old date while retaining history;
- a reply may resolve a `kind=waiting` item only when it directly supplies or declines the awaited result;
- an outgoing message does not prove the recipient completed their work;
- source disappearance does not imply cancellation or completion;
- a model may recommend `create|update|resolve|cancel|none`, but deterministic code checks source lineage and allowed transitions;
- no critic or candidate-by-candidate resolver loop runs in the operational path;
- malformed/timeout output causes no transition and marks connector coverage incomplete;
- human dismissal prevents same-version resurrection;
- reconciling duplicates retains a canonical survivor, resolves duplicates with explicit canonical-item metadata, and never deletes history.

## Calendar-First Connector

Calendar is the first production source because its schedule, identity, updates, and cancellation state are structured and require no LLM for the core briefing.

The first connector:

- is read-only;
- requests the exact scope [`https://www.googleapis.com/auth/calendar.events.owned.readonly`](https://developers.google.com/workspace/calendar/api/auth) plus existing identity scopes;
- reads only the owned primary calendar in the initial slice, even though the grant can read events on other calendars the operator owns;
- uses an explicit calendar-ID allowlist if additional owned calendars are later enabled;
- requires a separate scope-widening decision before reading a shared calendar that the operator does not own; the broader `calendar.events.readonly` scope is not part of the initial grant;
- does not request calendar-list discovery scope unless separately approved;
- stores tokens in macOS Keychain and non-secret account/config status locally;
- uses incremental sync tokens after a bounded initial window;
- handles an invalid/expired sync token with a bounded resync;
- normalizes source-native created/updated/start/end/timezone/status/recurrence/attendee-response fields;
- stores no private extended properties unless explicitly required and approved;
- never creates, edits, deletes, accepts, or declines an event.

The default initial window is 14 days in the past and 90 days in the future. Configuration may narrow it. Expanding it requires an explicit storage/privacy reason.

Calendar normalization and reconciliation MUST correctly handle:

- recurring series and individual exceptions;
- moved instances and original start time;
- cancelled instances and restored events;
- all-day dates across local timezone changes;
- daylight-saving transitions;
- RSVP changes without treating attendee lists as speaker identity;
- duplicate/mirrored calendars through provider identity, not title matching;
- confidential/private events with bounded display text;
- event updates that arrive out of order.

Today/upcoming event cards are deterministic. A model is not required to restate an event title or decide that an event exists.

## Gmail Lanes

Gmail remains `auth_only` until the owner separately approves message scopes, redaction, retention, deletion, and production capture. The benchmark credential is not authorization for the Brain connector.

Once approved, Gmail has three independent lanes over one normalized thread snapshot:

| Lane | Admission | Purpose |
|---|---|---|
| retrieval | approved normalized human, bulk, and transactional mail | answer targeted questions from local evidence |
| knowledge | likely durable human/evidence threads | run the existing fact curation pipeline |
| operations | changed threads with possible current work, including transactional logistics | detect and reconcile operational items |

The knowledge filter and operational filter are orthogonal. Bulk/transactional mail MUST NOT be excluded from operations merely because it is ineligible for durable facts. Marketing and repetitive notifications may be deterministically suppressed.

The shared normalization contract remains:

- one snapshot-replaced document per thread;
- Gmail internal message dates preserved;
- quoted reply history removed;
- attachment bytes excluded by default;
- HTML reduced to bounded text only when plain text is absent;
- source/message/thread identifiers retained for provenance;
- redaction occurs before persistent normalized storage;
- no send, modify, delete, label, or attachment-fetch behavior in the read-only phase.

### Operational Detector

The operational detector receives only:

- new/changed message text and bounded thread context;
- source/thread/message identity and dates;
- a bounded list of plausibly related active items;
- the closed item kinds, owners, state hints, and transition vocabulary.

It returns bounded structured candidates containing a detector-local key, kind, title, owner, optional dates, direct evidence references, confidence, and one recommended `create|update|resolve|cancel|none` operation.

V1 permits at most one semantic detector pass per changed thread, or one request containing a bounded batch of short threads. It has:

- no fact extractor prompt;
- no fact critic;
- no per-candidate resolver;
- no truth-maintenance loop;
- no automatic retry that multiplies calls for every ambiguity.

Malformed output produces no items and remains retryable on a later connector run. Ambiguity becomes an active low-confidence item with provisional/ambiguous reconciliation metadata, or no item, not an adjudication request.

Daily request/input/token budgets are explicit configuration. Overflow is deferred with visible coverage status; it is never silently dropped. Production enablement requires a chronological replay demonstrating that the approved budget covers normal and high-volume days.

## Briefing Contract

The briefing is a deterministic ranked projection as of a declared local time. It is generated on demand and by a configurable morning schedule.

Sections are:

1. `now_and_next`: current and upcoming Calendar events;
2. `overdue_and_due`: active commitments/follow-ups/deadlines ordered by urgency;
3. `waiting`: active `kind=waiting` items;
4. `attention`: changed, uncertain, high-priority, or decision-bearing items;
5. `low_confidence`: active items whose confidence/reconciliation metadata is provisional or ambiguous, visually separated from trusted work;
6. `system_coverage`: connector freshness, deferred volume, and failures.

Ranking is deterministic over:

- critical/high priority;
- overdue/due/start time;
- direct operator ownership;
- new cancellation/reschedule or material change;
- configured goal/project relevance;
- freshness and confidence;
- explicit operator pin/snooze feedback.

An optional model MAY compress wording after the ordered item set is fixed. It may not add items, dates, owners, priority, or completion state.

Every briefing item exposes:

- current state and freshness;
- why it appears now;
- due/start/expiry with source timezone where relevant;
- confidence and reconciliation status;
- evidence/source navigation;
- local `confirm`, `done`, `dismiss`, `snooze`, and correction actions that apply to its kind/state.

Briefing generation MUST NOT fail closed on one source. It returns `complete|partial|unavailable` plus per-connector coverage. Stale data may remain visible only with its last-updated time and an explicit stale label.

## Today UI

Today becomes the V1 Chief-of-Staff surface without changing navigation:

- the briefing leads the page;
- daemon, scheduler, index, sync, and review health remain a compact system pulse;
- item actions write only through the operational service and append an event;
- source links open the owning Brain evidence view when available;
- a refresh shows the new `as_of` time and rejects late responses from older generations;
- sidebar/menu badges do not count low-confidence or ambiguous items as confirmed obligations;
- notification text contains counts/status by default, not mail or calendar contents.

No separate Work, Goals, Approvals, or Chief Of Staff destination ships before shadow-mode evidence shows that Today cannot support the workflow.

## Evaluation And Promotion Gates

Shadow mode produces predictions, not labels. Evaluation MUST include human labels for surfaced items and stratified samples of suppressed sources.

The local labeled corpus remains private and outside git. It includes:

- chronological Calendar changes including recurrence and timezone cases;
- chronological Gmail threads across human, bulk, transactional, and marketing classes;
- low/median/high-volume days;
- threads/events with updates, cancellation, moved deadlines, completion, and no operational content;
- held-out dates not used to tune prompts or rules.

### Required Metrics

Detection:

- item precision and recall;
- critical/high-priority recall;
- false alarms per briefing;
- suppressed-source miss rate;
- classification by source class.

Reconciliation:

- duplicate-active-item rate;
- false-merge and false-split rate;
- stale-active-item rate;
- reschedule/cancellation/closure accuracy;
- resolved-item resurrection rate;
- time from source update to canonical item update;
- human correction and dismissal rate.

Briefing:

- high-priority recall in the appropriate section;
- stale or terminal items shown as active;
- ranking usefulness from operator feedback;
- daily item churn and repeated false alarms;
- incomplete-coverage disclosure accuracy.

Cost and reliability:

- requests and total/cached/uncached tokens per day, thread, and accepted item;
- p50/p95 detector latency;
- malformed/timeout rate;
- deferred volume and age;
- operational DB write latency, lock retries, and size growth.

### Minimum Gates

Calendar shadow promotion requires:

- 100% identity correctness for labeled recurring instances;
- 100% labeled cancellation/reschedule application correctness;
- zero duplicate active items from replaying the same source version;
- zero silent freshness/coverage failures.

Gmail live briefing promotion requires, on held-out chronological data:

- critical/high-priority recall at least `0.95`;
- overall item precision at least `0.80`;
- false-merge rate at most `0.01`;
- duplicate-active-item rate at most `0.05`;
- stale-active-item rate at most `0.05`;
- resolved-item resurrection rate at most `0.01`;
- all suppressed categories represented in manual labels;
- owner-approved daily token/call budget with no silent overflow.

At least 30 chronological days, including representative high-volume days, are required before a Gmail shadow result may be promoted. A metric regression disables Gmail-derived briefing items without disabling retrieval indexing or Calendar.

## Guarded External Execution

External execution is a later capability layer. It does not reuse the Knowledge Curation `cos_actions` ledger because external systems have different authority, drift, and reversibility semantics.

Every capability declares exactly one reversibility class:

| Class | Meaning | Example |
|---|---|---|
| `read_only` | no external mutation | read Calendar events |
| `reversible` | exact original state can be restored under an upstream precondition | rare provider operation with versioned conditional undo |
| `compensable` | an inverse can be attempted but observers/effects cannot be erased | edit a Calendar event after invitees may have seen it |
| `irreversible` | no meaningful inverse exists | send email |

The action lifecycle is:

```text
draft locally
  -> plan exact capability/account/target/payload
  -> read and hash before state
  -> bind approval to before hash + write + verify + compensation plan
  -> approve through a human-only local surface
  -> re-read and abort on drift
  -> commit with idempotency key when supported
  -> verify external result
  -> store after state and append tamper-evident audit event
  -> compensate only after matching the recorded after state
```

Requirements:

- planning and drafting do not mutate the provider;
- the agent-facing surface cannot approve its own action;
- approval expires and is invalid if payload, target, account, before state, verify plan, or compensation plan changes;
- commit refuses unapproved, expired, or drifted plans;
- upstream conditional writes/version fields are used when available;
- verification failure after a write preserves the write result and recovery evidence;
- compensation never claims to be a database rollback;
- manual cleanup is recorded as external reconciliation, not fake rollback;
- irreversible actions display that class at plan and approval time;
- send-email is never the first enabled write capability and requires a separately approved release/spec update.

Activation order is fixed:

1. read-only operational ledger;
2. shadow briefing;
3. local draft-only actions;
4. one explicitly approved compensable capability;
5. additional capabilities individually gated by eval, approval UX, and recovery drills.

## Naming Boundary

The operational Chief Of Staff owns `ops_*` concepts. Existing `cos_actions`, `cos_policy`, `cos_audit`, `config/local/cos_llm.yaml`, and `brain cos` names describe the implemented Knowledge Curation system and are legacy implementation names.

They MUST NOT be reused or extended for operational items or external action plans. Their eventual rename to `curation_*` is all-or-nothing across schema, modules, CLI, configuration, API, tests, and docs. Until that dedicated migration lands, documentation must identify them as Knowledge Curation compatibility names rather than call them the operational Chief of Staff.

The existing user-facing Ops destination continues to mean system/runtime operations in V1.

## Privacy And Security

- Connector credentials and refresh tokens remain in macOS Keychain.
- Each connector requests the narrowest approved read scope; write scopes are absent before guarded-execution activation.
- Account IDs stored in SQLite are local non-secret identifiers.
- Full provider API caches are not part of the production contract.
- Attachment bytes are excluded by default.
- Model payloads contain only the changed source material and bounded related-item context required for the decision.
- Provider use is explicit per operational detector role; unconfigured roles skip visibly.
- Raw model prompts/responses are not retained by default. Debug retention is opt-in, private, bounded, and reported in storage inventory.
- Briefing caches contain item IDs/ranking metadata rather than copied source bodies and expire after 30 days by default.
- Source forgetting/redaction must invalidate or redact linked operational evidence and may leave a tombstoned item event explaining the loss of evidence.
- No private briefing content appears in notifications by default.
- No analytics leave the machine.

## Failure Semantics

- A connector failure marks its coverage stale/failed and does not abort other sources.
- Provider timeout or malformed detector output produces no transition and cannot resolve existing work.
- Deferred budget overflow is counted and aged; it is not reported as complete coverage.
- Out-of-order source updates cannot overwrite a newer provider version.
- Missing evidence renders an explicit unavailable state; it does not fabricate a quote or date.
- Clock/timezone ambiguity remains explicit and cannot create a precise deadline.
- Operational DB failure does not block knowledge ingest/retrieval/curation.
- Knowledge DB unavailability prevents evidence enrichment but does not permit an ungrounded operational mutation.
- A failed briefing generation preserves the prior briefing only as visibly stale and returns the failure reason.
- Any external-write ambiguity, drift, approval mismatch, or verification failure stops further mutation and leaves an auditable recovery state.

## Non-Goals

- A second Chief-of-Staff app, daemon, auth store, or scheduler.
- Treating every email, calendar event, or extracted fact as an operational item.
- Running the fact extractor, critic, resolver, or gardener over all mail for operational detection.
- A general task/project-management replacement.
- Inferred or model-authored goals in V1.
- A separate state table and workflow for every item kind.
- Automatic external writes before shadow, draft, approval, drift, verification, and recovery gates pass.
- Gmail sending, modification, deletion, label mutation, or attachment ingestion in the read-only phases.
- Navigation redesign before Today proves the loop.
- Live multi-writer replication of either SQLite database.

## Acceptance

The operational Chief-of-Staff foundation is complete when:

1. one app/daemon initializes and serves both knowledge and operational stores without cross-database write transactions;
2. Calendar read-only capture and replay produce idempotent event items across recurrence, moves, cancellations, all-day dates, and timezone changes;
3. `ops_items` and append-only events preserve every automated and human transition;
4. Today generates a freshness-stamped briefing with evidence, confidence, reasons, and connector coverage;
5. local completion, dismissal, snooze, and correction survive restart/replay and prevent same-version resurrection;
6. knowledge facts, curation Queue, and `cos_*` compatibility paths remain behaviorally unchanged;
7. Calendar promotion gates pass before Gmail content access is enabled;
8. Gmail retrieval, knowledge, and operational lanes have separate admission and cost reporting;
9. Gmail shadow evaluation includes labeled suppressed mail and passes detection, duplicate, staleness, reconciliation, and budget gates;
10. an operational provider failure cannot complete or cancel an existing item;
11. operational DB lock/failure cannot corrupt or block the knowledge DB;
12. backup/restore preserves both current item state and human feedback history;
13. no external mutation scope or action is enabled by the read-only/shadow implementation;
14. any later write capability declares a reversibility class and passes payload-bound approval, drift, verification, audit, and recovery acceptance.

Primary planned verification surfaces:

```bash
uv run pytest tests/test_ops_store.py tests/test_ops_reconciliation.py \
  tests/test_calendar_connector.py tests/test_ops_briefing.py -q
uv run brain eval run --suite operations --home <test-home>
swift test --package-path app
scripts/build-app.sh
```

The implementation plan owns release sequencing. This spec owns the behavior and promotion gates.
