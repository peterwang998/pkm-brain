# Chief Of Staff Operations

**Status:** canonical feature spec; the manual Calendar/Gmail shadow implementation and operator-feedback tranche are locally release-verified and installed, while owner visual UX review and every empirical promotion gate remain pending
**Last verified:** 2026-07-14 with Ruff and diff checks green, 772 Python tests, 28 Swift tests, and a signed local app bundle installed with healthy runtime fingerprint `ac389246`; the latest provider run completed Calendar but stopped Gmail before fetch at the approved `1200/1200` daily API cap, while an isolated production-code replay of the exact retained 200-thread page completed without an observation conflict or visible marketing leak
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

## Current Shadow Trial State

The provider-reading evaluation slice runs only when the owner chooses **Today > Run Shadow**. It requires two separately authorized Google grants: the owned-primary Calendar grant and the Gmail read-only grant. Immediately before every run, the daemon binds each grant to the exact policy email, stable provider identity when available, and exact approved scope set. A missing, broader, changed-account, or mismatched grant fails closed.

The first accepted run creates the owner-only `config/local/operations.yaml` from the approved defaults only when no policy exists. An existing policy is authoritative and is never silently rewritten. The approved defaults are:

- owned primary Calendar only;
- Gmail read-only content access for the operational shadow lane only;
- 7-day raw resumable-cache retention and 30-day normalized-evidence retention;
- no attachment fetching, quoted-history stripping enabled, and no external provider writes;
- daily Calendar API, Gmail API, detector-call, detector-input-token, and detector-total-token budgets.

One asynchronous run reads Calendar and Gmail independently, reconciles bounded evidence into `ops.sqlite`, saves a coverage-aware briefing snapshot, and exposes progress plus a prominent terminal outcome through Today. A second start while a run is active reuses the active run rather than starting overlapping source work. Partial pagination and bounded deferred work remain resumable through persisted source cursors; a later manual run continues from the last atomically committed checkpoint. A terminal projection or snapshot failure preserves already-completed source coverage, usage, counts, and cursor progress rather than replacing them with an undifferentiated failure.

Calendar/Gmail capture remains manual and is not scheduled. Once the operational store exists, a daemon-local job may prepare derived briefs from already retained Calendar and Brain evidence; it does not poll Google or invoke the Gmail detector. The slice does not write `inbox/`, `brain.sqlite`, documents, chunks, facts, entities, or wiki pages. Gmail retrieval indexing and durable-knowledge ingestion remain disabled. Feedback, missing-item reports, observations, current items, suppression preferences, prepared packets, and briefing snapshots are local operational records only. The owner—not an agent or background job—authorizes both grants and starts the first live run.

Implementation completion and one live validation do not equal promotion. Human review plus a larger private trial must still establish labeled recall, false-alarm, duplicate, stale, handled-state, evidence-route, coverage, and cost behavior before the briefing can be treated as trusted daily operational guidance.

Local release verification is complete: Ruff and diff checks are green, 772 Python tests and 28 Swift tests pass, and the signed local app bundle builds, installs, launches, and serves healthy runtime fingerprint `ac389246`. The proactive `executive-brief-v2` packet was verified as prepared in advance by the completed scheduler, and live hide/Undo behavior was verified for the recurring `Family Time` series.

The latest installed provider run completed Calendar, then correctly stopped Gmail before any fetch because the approved durable daily API budget was already `1200/1200`. It therefore does not prove a complete detector-v6 provider/cursor run. To exercise the repaired classification and immutable-observation path without bypassing that budget, an isolated production-code replay used the exact retained 200-thread page: zero model calls, 51 marketing threads suppressed, 7 already-tracked marketing threads kept hidden while pending reconciliation, 5 bulk threads suppressed, 3 recruiter threads filed as Attention, 134 model-dependent threads deferred, 10 plausible threads retained as Uncertain, and 3 derived observations applied. No marketing item leaked into a visible section and no `ObservationConflictError` occurred. These results validate deterministic routing, budget behavior, and versioned observation storage; they do not establish model judgment quality or promote either connector. Owner visual review, human labeling, and every empirical Calendar, Gmail, cross-source, meeting-preparation, and daily-briefing promotion gate remain pending.

The owner-facing procedure is [Live Chief-of-Staff Shadow Trial](../runbooks/chief-of-staff-shadow-trial.md). Offline chronological scoring remains documented separately in [Retrospective Shadow Replay](../runbooks/chief-of-staff-shadow-replay.md).

## V1 Scope

V1 proves one closed loop:

1. load an explicit local operations policy for identity, responsibility, ranking, and source selection;
2. capture read-only Calendar evidence through a typed incremental source adapter;
3. reconcile it into current operational state;
4. generate an evidence-backed Today briefing with an adaptive focus set;
5. accept local corrections and completion/dismissal feedback;
6. measure duplicate, stale, missed, false-alarm, focus-selection, and evidence-link rates;
7. add read-only Gmail operational detection and source-local action-satisfaction checks only after the Calendar loop passes;
8. add reversible cross-source episode linkage and cross-source satisfaction checks only after both source-local paths are trustworthy.

V1 keeps the existing seven-destination navigation. Today becomes the briefing surface. Queue remains the Knowledge Curation review workflow, and Ops remains the daemon/system-administration destination.

Goals are operator-authored intent, not extracted observations. V1 MAY read an explicitly configured human-authored Markdown page, defaulting to `wiki/goals.md`, as ranking context. It MUST NOT create a goal table or infer goals from mail.

## Local Operations Policy

The Chief of Staff uses an explicit, versioned, local policy rather than inferring the operator's identity, responsibilities, or priorities from traffic volume. The default private configuration path is `config/local/operations.yaml`. It is not checked into source control, contains no credentials, and is validated before any configured value affects selection or ranking.

The policy may declare:

- stable operator identities per connector, including email addresses, provider user IDs, Git identities, and approved aliases;
- owned, shared, and adjacent responsibility areas;
- configured accounts, calendars, projects, repositories, people, and organizations;
- local timezone, working hours, briefing schedule, and pre-meeting preparation window;
- deterministic priority and exception rules, including legal, financial, security, safety, travel, and direct-commitment exceptions;
- pointers to operator-authored goal pages and other approved ranking context;
- connector-specific allowlists and provider-native host/tenant mappings used to construct safe source links.

Stable provider IDs take precedence over display names. A display name alone MUST NOT prove operator identity, authorship, assignment, or reply status.

Responsibility is a ranking signal, not a destructive admission filter. Out-of-area material is normally demoted, but a direct obligation or a configured high-consequence exception remains eligible. Unknown responsibility remains explicit; the system does not silently assign ownership or discard the item. Every persisted briefing or handled-state assessment records the policy version that influenced it so results are replayable after policy changes.

### Shadow policy schema V1

The first live-evaluation policy is intentionally narrower than the eventual multi-source policy. It has independent `schema_version`, `policy_id`, and positive `policy_version` fields and accepts only `mode=shadow_read_only`. The loader rejects unknown or credential-like fields, invalid timezones, symlinks, and group/world-readable policy files before a connector may use the policy.

V1 requires:

- stable Calendar and Gmail email identities with distinct local `account_key` values;
- Calendar limited to `calendar_id=primary`, `ownership=owned`, and `calendar.events.owned.readonly`;
- Gmail limited to `gmail.readonly`, with an additional explicit `content_access_approved` value before it may be enabled;
- raw resumable cache retention of 7 days and normalized evidence retention of 30 days;
- attachment fetching disabled, quoted reply history stripping enabled, and external provider writes disabled;
- positive daily Calendar request, Gmail request, detector-call, detector-input-token, and detector-total-token budgets;
- disjoint owned/shared/adjacent responsibility areas, out-of-area demotion rather than exclusion, explicit unknown responsibility, and direct-obligation eligibility;
- legal, financial, security, safety, travel, and direct-commitment high-consequence categories that remain eligible and cannot be auto-suppressed.

Disabling Gmail is valid and does not imply content authorization. Enabling it without explicit approval is invalid. A later policy schema may add sources or ranking controls, but it must migrate explicitly rather than accepting unknown V1 fields.

## Source Adapter Contract

Every operational source implements the same failure-isolated, incremental adapter boundary. An adapter emits bounded typed source-unit batches; it does not write current item projections directly and does not depend on an unbounded model context or a transient copied-source corpus.

Each adapter contract declares:

- connector and adapter version, account/tenant identity, source type, stream key, object key, revision, replay-stable authority order, and provider update time;
- initial window, incremental cursor, pagination, tombstone, bounded resync, retry, and rate-limit behavior;
- approved scope, allowlisted sources, redaction, retention, and deletion behavior;
- stable evidence references plus a local evidence route and, when available, a validated provider-native route;
- which native signals can establish authorship, assignment, response, delegation, fulfillment, cancellation, or current authoritative state;
- per-run budgets, deferred-work accounting, freshness, and `complete|partial|unavailable` coverage;
- deterministic normalization and fixture replay behavior before any optional detector is invoked.

Adapters run independently so one large or unavailable source cannot consume another source's context or erase its result. Parallel polling is allowed, but each source-unit batch and cursor commit remains atomic under the operational writer.

A notification or copied summary is a lead, not the authority for an upstream object. When an email or collaboration message points to a ticket, pull request, build, reservation, invoice, or other canonical object, the current state from that authoritative provider outranks the notification text. If the authoritative adapter is absent, unauthorized, stale, or failed, the Chief of Staff may surface the lead but MUST report verification as unknown and MUST NOT infer completion or an all-clear.

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

## Model Roles And Decision Authority

There is no single model behind the Chief of Staff. Source normalization, Calendar identity and lifecycle, operational reconciliation, handled-state rules, visible-section assignment, ranking, focus selection, suppression, and cached-packet validity are deterministic. That code remains authoritative even when a model proposes a candidate.

The default Chief-of-Staff generative configuration is `gpt-5.6-luna` at `high` reasoning. The installed selection has been verified as that model and effort through the restricted, tool-less Codex route. The current generative-model role is deliberately narrow: the Gmail operational detector inherits those shared Chief-of-Staff defaults; an explicit Gmail-specific configuration or environment override may replace the inherited model or effort. It receives one bounded changed thread or bounded batch and returns schema-validated suggestions. The model cannot browse, call tools, mutate providers, select the final briefing, or adjudicate lifecycle state. Its effective model, effort, configuration source, provider route, prompt, detector version, and usage remain versioned evaluation inputs; changing any of them requires replay and cost/quality review.

Calendar cards, the Today projection, recurring-series suppression, and the current meeting-preparation packet do not use a generative model. Meeting preparation uses deterministic structure plus approved local Brain retrieval to organize source-backed material. A later bounded wording stage MAY improve prose only after deterministic selection has fixed the claims, links, ordering, and coverage. It MUST NOT add or remove operational items, factual claims, people, dates, owners, priority, handled state, or evidence.

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
resolved  -> active | cancelled    only from materially newer evidence or human restore
expired   -> active | cancelled    only from materially newer evidence or human restore
dismissed -> active | cancelled    only through explicit human restore or a new episode
cancelled -> active                only when the provider restores/reschedules it
```

The `cancelled` target from `resolved|expired|dismissed` is valid only when an explicit human restore removes the local override and the current direct provider observation is already cancelled.

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
| `source_type`, `stream_key`, `source_key` | exact provider source-unit and object identity |
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
| `human_confirmed_at`, `last_human_action_at` | sticky operator-override provenance |
| `metadata` | validated owner/counterparty, evidence, reconciliation-status, source-version, snooze, and resolution detail |
| `created_at`, `updated_at` | lifecycle timestamps |

Dates are optional. The system MUST NOT invent a due date because an email says “soon” or because a model expects one. Unknown owner, date, or project remains explicit.

### Event History

`ops_observations` stores immutable normalized source revisions. Each row records source type, account key, stream key, source key and opaque revision, a non-negative connector-supplied provider-authority order, optional provider source-update time, observation time, proposed item kind, `upsert|cancelled` event kind, validated payload, content hash, bounded stable evidence references, and creation time. Present operational timestamps are canonical UTC; `source_timezone` preserves the rendering context. A sparse provider tombstone may omit `source_updated_at`; the connector MUST NOT invent a wall-clock provider time. Replaying the same source revision and canonical content is a no-op. A conflicting replay of the same revision is an error. Revision strings and content hashes MUST NOT be used as chronology. The implemented Gmail path separates the immutable derived interpretation revision from the raw provider revision: the derived revision is keyed by raw provider revision, detector version, and the active policy version, while the raw provider revision remains separately cited in evidence. `policy_version` is allowed but not required in observation metadata so legacy observations remain valid. Detector or policy upgrades therefore append a new interpretation without rewriting legacy history, while divergent output under the same declared interpretation identity still fails closed.

`source_order` is scoped to one source type/account/stream and must be replay-stable. It derives from an authoritative provider sequence or from the persisted ordinal of a provider change-set committed with the source cursor. It MUST NOT derive from poll wall-clock time, response page position, lexical revision/etag order, or content-hash order. Projection authority compares `(source_order, source_updated_at-or-minimum)`; a distinct observation that does not sort after the current one is retained with `stale_ignored` and cannot mutate current state.

Observation metadata uses a closed `schema_version=1` scalar-key contract. V1 admits only `all_day`, `attendee_count`, `attendee_response`, `calendar_id`, `detector_version`, `event_type`, `ical_uid`, `location`, `message_class`, `organizer_self`, `original_start_time`, `policy_version`, `provider_sequence`, `reconciliation_status`, `recurring_event_id`, `source_status`, `transparency`, and `visibility`. Arbitrary keys, nested source payloads, descriptions, snippets, HTML, or message/event bodies are rejected. Full provider payloads remain in the separately governed evidence/capture layer, not `ops.sqlite`.

`ops_item_events` is an append-only technical history, not a second domain model. Each event records:

- event ID and item ID;
- `created|updated|rescheduled|cancelled|resolved|expired|stale_ignored|feedback` event type;
- actor class `connector|deterministic|model|human`;
- source reference and source version where applicable;
- prior and resulting state;
- run ID and reconciliation version;
- bounded structured transition detail and its hash;
- event timestamp.

`ops_source_cursors` stores replay-safe connector/account/stream cursors, watermarks, bounded metadata, last-success time, and a monotonically increasing local generation. A source batch is bound to exactly one source type/account/stream. Its cursor advances only in the same transaction as every observation/item/event write for that source unit. Compare-and-swap checks both the prior cursor and generation, including cursorless watermark streams.

A first-seen provider tombstone creates a terminal item and a `created` event with `source_event=cancelled`; cancellation of an existing item emits `cancelled`. If a human `resolved|dismissed` override exists, direct provider cancellation still advances the immutable current observation and appends the cancellation event, but it does not replace the sticky human state. Explicit restore then projects the current source as `active|cancelled`. Human feedback events reference the immutable current observation/version they supersede without creating a synthetic provider observation.

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

Briefing snapshots are derived. They MAY be cached in `ops.sqlite` with generation time, horizon, ordered item IDs, ranking reasons, completeness, and connector coverage, but MUST NOT duplicate source bodies. The serialized sections field has an immutable 256 KiB storage ceiling; generation MUST compact and fairly bound the user-visible projection to at most 240 KiB, leaving storage headroom. Bounding preserves each section's true `total`, preview `included`, and `omitted` counts, so truncation cannot become a false all-clear. Complete operational history remains in the canonical item, observation, event, and decision tables rather than being copied into the briefing. Cached briefing snapshots expire after 30 days by default.

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
- the current Gmail detector may recommend only `create|update|needs_reconciliation`; deterministic code owns every lifecycle effect and downgrades unsupported or ambiguous operations;
- no critic or candidate-by-candidate resolver loop runs in the operational path;
- malformed/timeout output causes no transition and marks connector coverage incomplete;
- human dismissal prevents same-version resurrection;
- reconciling duplicates retains a canonical survivor, resolves duplicates with explicit canonical-item metadata, and never deletes history.

### Action Satisfaction And Handled State

Lifecycle state answers whether an operational item remains active. Handled state answers whether the operator still owes the next move. They are related but MUST NOT be conflated.

For obligations and attention items, the briefing derives one handled verdict:

| Verdict | Meaning |
|---|---|
| `needs_action` | evidence indicates that the operator still owns the next move |
| `responded_waiting` | the operator responded, but fulfillment now depends on another event or party |
| `being_handled` | another identified party has accepted or is demonstrably progressing the next move |
| `fulfilled` | direct evidence satisfies or declines the requested result |
| `unknown` | evidence or source coverage is insufficient to decide safely |

A handled verdict is a versioned derived assessment, not a sixth item state and not a provider observation. Each assessment records item/episode ID, verdict, supporting and contradicting evidence references, sources checked, per-source coverage, method/version, policy version, confidence, and `as_of`. It may be cached as bounded validated projection metadata, but it is recomputed when relevant source evidence, relations, policy, or coverage changes.

Verification rules:

- read/unread, seen, opened, or viewed state is only a weak attention signal and never proves fulfillment;
- an outgoing reply proves at most that a response occurred; a promise to act leaves the commitment active;
- a response resolves a waiting item only when it directly supplies, performs, or explicitly declines the awaited result;
- delegation may yield `being_handled`, but it does not resolve the operator's accountability unless policy and direct evidence establish that transfer;
- progress by another party may yield `responded_waiting|being_handled`; silence never does;
- any source failure or unverified authoritative object that could change the verdict forces `unknown` rather than a verified all-clear;
- a handled assessment may recommend an allowed transition, but only deterministic reconciliation or explicit human feedback mutates item lifecycle state.

Gmail first implements these checks inside one thread using stable operator email identities and message lineage. Cross-source handled verification is a COS-6 capability and cannot be claimed from semantic similarity alone.

### Cross-Source Episode Relations

COS-6 may group evidence and items from different sources into one operational episode without destructively merging their source identities or histories. Relations are explicit, evidence-backed, reversible assertions such as `same_episode`, `duplicate_of`, `responds_to`, `fulfills`, `delegates`, or `supersedes`.

Each relation binds exact endpoint IDs, relation type, supporting evidence, method/version, policy version, confidence, creator, and status. Automated relations begin proposed unless deterministic provider/business identifiers prove the link. Confirmation, rejection, and retraction append auditable transitions; they never delete either endpoint. A human-rejected relation cannot be recreated from the same evidence/version, and retracting a relation causes affected handled-state and briefing projections to be recomputed.

The initial COS-6 schema MAY add `ops_episode_relations` plus append-only relation events after a dedicated migration review. It does not create a second task ontology. False merge is more severe than a temporary duplicate: ambiguous candidates remain separate, and one focus card may aggregate an episode only when the active relation meets the approved confidence/confirmation gate.

## Calendar-First Connector

Calendar is the first operational source because its schedule, identity, updates, and cancellation state are structured and require no LLM for the core briefing.

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

Calendar authority mapping is explicit:

- `stream_key` is the canonical calendar ID and source identity includes the account, calendar, event ID, and recurring-instance original start where applicable;
- `source_revision` is the event etag when present; for an ID-only deletion tombstone it is a deterministic hash of the provider sync checkpoint plus the minimal tombstone identity, used only for idempotence;
- `source_order` is the next locally committed per-calendar source-unit/cursor generation; accepted partial continuation tranches advance that local generation, while the final provider [`nextSyncToken`](https://developers.google.com/workspace/calendar/api/guides/sync) is committed only with complete coverage. It is never API page order or poll time;
- `source_updated_at` is the Calendar event `updated` value when present, while the provider `sequence` value is retained as bounded metadata; sparse tombstones may leave both absent because [Google guarantees only minimal identity fields for some cancelled events](https://developers.google.com/workspace/calendar/api/v3/reference/events#resource).

The connector commits each bounded continuation tranche as one atomic source unit: observations, decisions, assessments, and the matching continuation checkpoint advance together. A replay from the last committed checkpoint reproduces the same revision/order inputs. Coverage remains partial while continuation is pending. An expired sync token marks coverage incomplete and triggers the bounded full-resync policy; absence from that resync never fabricates a cancellation.

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

The operator may hide an entire recurring Calendar series from Today when it is a personal block, routine, or other non-meeting. This is a reversible local projection preference keyed by account plus the provider recurring-series ID. It hides current and later retained occurrences from Today and from proactive meeting preparation without dismissing the underlying items, deleting evidence, or writing to Google Calendar. Today MUST retain a compact **hidden recurring series** disclosure with the number of hidden occurrences and an **Undo** action. One-off events cannot create a series rule, and restoring a rule makes eligible occurrences visible again immediately.

## Gmail Lanes

The owner has approved `gmail.readonly` plus the 7/30-day retention, no-attachment, quoted-history-stripping, and no-external-write controls for the private operational shadow trial. This approval does not authorize Gmail knowledge capture, retrieval indexing, durable-fact extraction, or any Gmail mutation. The benchmark credential is not authorization for either Brain lane; the live trial requires its own separately authorized and account-bound Brain grant.

Gmail has three independently permissioned lanes over a normalized thread snapshot:

| Lane | Admission | Purpose |
|---|---|---|
| retrieval | approved normalized human, bulk, and transactional mail | answer targeted questions from local evidence |
| knowledge | likely durable human/evidence threads | run the existing fact curation pipeline |
| operations | changed threads with possible current work, including transactional logistics | detect and reconcile operational items |

The knowledge filter and operational filter are orthogonal. Bulk/transactional mail MUST NOT be excluded from operations merely because it is ineligible for durable facts. Advertising, newsletters, promotions, and other marketing updates are deterministically suppressed before broad action or high-consequence keyword matching, remain available only in the collapsed suppression audit, and MUST NOT spill into the uncertainty section because boilerplate contains urgency words. That gate MUST NOT strand an already tracked thread: a thread with a current operational item remains detector-eligible when its source revision changes. Transactional logistics, payments, renewals, travel, security, and other current obligations remain independently eligible.

Individual recruiter outreach, interview scheduling, application follow-up, and similar human recruiting activity are operationally relevant. Routine recruiter activity is filed under `attention` at normal priority and does not consume a Focus slot merely because it is new. Exact evidence for a commitment, deadline, or scheduled time preserves that stronger item kind and normal ranking. Bulk job alerts and job digests remain marketing updates and stay hidden by default.

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

It returns bounded structured candidates containing a detector-local key, kind, title, owner, optional dates, direct evidence references, confidence, and one recommended `create|update|needs_reconciliation` operation. The model cannot directly resolve, cancel, complete, or suppress an existing item; deterministic reconciliation and explicit operator feedback own those effects.

V1 permits at most one semantic detector pass per changed thread, or one request containing a bounded batch of short threads. It has:

- no fact extractor prompt;
- no fact critic;
- no per-candidate resolver;
- no truth-maintenance loop;
- no automatic retry that multiplies calls for every ambiguity.

Malformed output produces no model-authored item. In a multi-thread response, validation is isolated per thread: one malformed or missing result cannot discard valid results for the other threads in that batch. Any failed admitted thread may instead receive a clearly labeled low-confidence attention fallback so detector failure is visible; direct and high-consequence obligations receive the stronger no-suppress treatment and internal ranking without being presented as verified work. The fallback decision is cached for that exact source revision to avoid repeated billing, and a newer revision is eligible for detection again. Deterministic ambiguity may otherwise produce a provisional item or no item, but never an adjudicated lifecycle effect.

Daily request/input/token budgets are explicit configuration. Each detector attempt durably pre-reserves a conservative, no-refund input and total ceiling before launch, and the only supported live provider is the restricted Codex route with its matching in-flight rollout cap. Provider-reported usage is recorded separately and any positive call/input/total delta is durably added after the attempt; missing usage or an observed overage stops later calls and makes coverage partial. Overflow is never silently dropped. Production enablement requires a chronological replay demonstrating that the approved budget covers normal and high-volume days.

The installed detector is `gmail-operations-v6`. Its latest provider run was stopped before Gmail fetch at the already-consumed `1200/1200` daily API cap, so provider pagination, cursor completion, and model judgment quality are not claimed from that run. An isolated production-code replay against the exact retained 200-thread page exercised detector-v6 deterministic admission, routing, and derived-observation persistence with zero model calls: 51 marketing threads and 5 bulk threads were suppressed, 7 tracked marketing threads remained hidden pending reconciliation, 3 recruiter threads entered Attention, 134 model-dependent threads were deferred, 10 plausible threads remained Uncertain, and 3 derived observations were applied. The replay produced no visible marketing leak and no immutable-observation conflict. This is release-path evidence, not a held-out quality result.

## Later Read-Only Adapters

After Calendar, Gmail, handled-state, and cross-source reconciliation gates pass, additional adapters may be enabled independently under the common source contract:

- local Git worktrees for branch divergence, uncommitted state, recent commits, and locally observable integration risk;
- approved code-host accounts for review requests, pull-request state, CI/build results, and merges;
- local agent-session history for unfinished tasks, recorded outcomes, explicit blockers, and recurring friction;
- collaboration systems for direct asks, mentions, thread progression, and stable-user-ID response evidence;
- work trackers for assignments, questions directed to the operator, status, due dates, and authoritative ticket history.

Local Git and agent-session summaries are evidence only for what they directly record. An agent-generated summary may suggest a lead, but it does not prove that an external task, review, or commitment is complete. Code-host build/review state and work-tracker ticket state outrank notifications about those objects. Collaboration response checks use stable provider user IDs rather than display names.

Each adapter requires its own scope/privacy decision, labeled fixtures, source-local identity and replay tests, budget, coverage reporting, and rollback-free read-only disable path. No adapter is bundled into another adapter's authorization, and a later adapter does not delay or weaken the Calendar/Gmail release gates.

## Briefing Contract

The briefing is a deterministic ranked projection as of a declared local time. Calendar/Gmail source refresh remains an explicit manual shadow action. Derived meeting briefs are prepared proactively from retained local evidence for eligible timed, non-transparent events in the next 72 hours: once after a Calendar shadow run and by the serial daemon scheduler every 15 minutes. All-day events, transparent events, and the high-precision normalized `Family time` personal-block title/prefix are excluded from proactive preparation, but remain visible in Today and available through explicit on-demand preparation. This local job performs no provider poll and no generative-model call. A configurable morning source-refresh schedule remains a later, separately activated capability and is not enabled by the manual trial.

Sections are:

1. `focus`: up to five distinct operational episodes that most need the operator's next move;
2. `urgent_overflow`: any additional critical/high-priority `needs_action` episodes that did not fit in focus;
3. `now_and_next`: current and upcoming Calendar events;
4. `overdue_and_due`: active commitments/follow-ups/deadlines ordered by urgency;
5. `waiting`: active `kind=waiting` items;
6. `attention`: confirmed decision-bearing or high-priority items that do not belong in a more specific action section;
7. `awareness`: relevant informational items that do not currently need the operator's action;
8. `low_confidence`: active items whose confidence/reconciliation metadata is provisional or ambiguous, visually separated from trusted work;
9. `system_coverage`: connector freshness, deferred volume, and failures.

Every operational item is assigned to exactly one primary user-visible item section. Facet counts may record that an item also matched other section predicates, but the same card is not repeated across Focus, Attention, Due, and Uncertain. Suppressed-source decisions are audit records rather than operational items and keep their own bounded, collapsed preview. Marketing updates remain in that audit and never appear as visible uncertain work.

Focus selection is adaptive rather than quota-filling:

1. rank a bounded candidate set of active, confirmed action-bearing items or confirmed episodes; an item below `0.65` confidence or with `provisional|ambiguous` reconciliation status is not an action candidate;
2. derive handled state using all authorized, fresh sources that could satisfy the action;
3. reject terminal, snoozed, duplicate-episode, and verified `fulfilled|being_handled` candidates from focus while retaining them in their appropriate full section when useful;
4. include at most five `needs_action` candidates, or fewer when fewer qualify;
5. disclose every additional critical/high-priority `needs_action` candidate in `urgent_overflow` rather than hiding it;
6. never pad focus with awareness material merely to reach five.

`unknown` handled state is never silently suppressed. A confirmed, sufficiently confident operator-owned or high-priority item may remain an action candidate when handled verification is unknown; an unverified, provisional, ambiguous, or low-confidence item appears only in the uncertainty section. Uncertain cards MUST NOT display verified-style `P0` or `P1` badges even when their internal urgency score is high. Zero focus items is a valid result only when coverage and satisfaction checks support it.

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
- who owns the next move and what evidence would satisfy it;
- handled verdict plus checked-source coverage where applicable;
- due/start/expiry with source timezone where relevant;
- confidence and reconciliation status;
- a source-backed named counterparty or organization when one is asserted; generic labels such as “customer” remain explicitly unidentified rather than being presented as a specific entity;
- bounded local evidence navigation and validated provider-native navigation where authorized;
- local `confirm`, `done`, `dismiss`, `snooze`, and correction actions that apply to its kind/state.

Briefing generation MUST NOT fail closed on one source. It returns `complete|partial|unavailable` plus per-connector coverage. Stale data may remain visible only with its last-updated time and an explicit stale label. The persisted preview MUST remain within the 240 KiB projection target under the 256 KiB storage ceiling and expose true total/included/omitted counts for every section.

### Evidence Navigation

Every factual card, handled assessment, and meeting-preparation claim references stable local evidence IDs. Provider-native links are optional conveniences, not evidence themselves. They are constructed from adapter-owned stable IDs and allowlisted scheme/host/tenant templates; arbitrary URLs from message or event text are never rendered as trusted source links. Links contain no credentials, refresh tokens, copied source bodies, or unredacted query text, and the UI identifies the account/provider before opening them.

When a provider object is unavailable or the route cannot be validated, the local evidence route remains available where retention permits and the provider link is omitted. Source deletion or redaction invalidates the corresponding route and visibly downgrades dependent assessments rather than leaving a misleading deep link.

## Derived Meeting Preparation

For a configured or operator-selected upcoming event, the Chief of Staff MAY generate a bounded preparation packet as a derived view. The initial Calendar/Brain version may use event metadata, explicit local operations policy, current operational items, and approved Brain retrieval, facts, entities, and pages. A later COS-6 version may add fresh evidence from linked communication, work-tracker, and code-host adapters.

A packet is a human-readable executive meeting brief, not a dump of retrieval internals. Its primary reading order is purpose and meeting context, relevant background, likely agenda/talking points, open questions/preparation, and relevant source links. Raw event claims, fact IDs, evidence IDs, source freshness, coverage, and retrieval diagnostics remain available in a collapsed **Sources & diagnostics** appendix at the end.

A packet may contain:

- objective, timing, attendees, location, and material schedule changes;
- commitments, waiting items, and next moves in both directions;
- prior decisions and durable facts, distinguished from current source observations;
- recent relevant correspondence and authoritative work-object changes;
- sourced risks, unresolved questions, and decisions needed;
- suggested talking points clearly labeled as suggestions rather than facts;
- validated local/provider evidence links, including relevant Calendar, Brain, and linked email sources when present;
- freshness, policy version, and per-source coverage.

Packets are precomposed for eligible, unsuppressed active Calendar events inside a 72-hour window after each Calendar shadow run and every 15 minutes while the primary/single daemon is running. Proactive eligibility requires a timed event that is not marked transparent and does not match the high-precision normalized `Family time` personal-block title/prefix; excluded events retain their Today card, recurring-series controls, and explicit **Prepare now** support without consuming proactive preparation work. Opening a ready brief reads the cache; **Prepare now** is also the fallback when no current packet exists. The cache is bound to the exact Calendar source revision and current packet-content version, so an event update or presentation-safety change makes the prior packet ineligible. Packets use the same source budgets and coverage semantics as the briefing, are capped at 256 KiB, expire after 30 days, and are never canonical state. They MUST query approved retrieval evidence when needed rather than depending only on fact admission and MUST NOT duplicate full source bodies. User-visible Brain background, open questions, and Wiki links require meaningful normalized term/entity overlap with the Calendar title or retained Calendar notes after generic meeting words and operator-identity-only matches are removed; rejected retrieval remains available only inside the collapsed diagnostics footer.

The implemented packet composer is deterministic and uses no generative model. A future bounded wording model MAY organize or compress the already selected supported material, but it may not invent or omit evidence-bearing claims, attendees, commitments, decisions, customer/entity identity, dates, ownership, or completion. The deterministic source set, section order, links, coverage, and appendix remain authoritative.

## Today UI

Today becomes the V1 Chief-of-Staff surface without changing navigation:

- the briefing leads the page;
- **Run Shadow** starts one manual read-only Calendar/Gmail pass and shows a prominent accepted, running, complete, partial, or failed outcome that remains visible after the terminal refresh;
- focus shows zero to five cards, with urgent overflow and awareness visibly separate;
- retained local evidence opens in an in-app evidence sheet, while the ignored/suppressed audit is collapsed by default and shows its true total plus bounded reason-code/source-reference preview and omitted count;
- daemon, scheduler, index, sync, and review health remain a compact system pulse;
- item actions write only through the operational service and append an event;
- source links open the owning Brain evidence view and may offer a separately labeled validated provider route when available;
- eligible upcoming events show **Open brief** when a revision-current packet has already been prepared and **Prepare now** only as the fallback; the brief opens without creating another navigation destination;
- recurring Calendar cards may hide their entire provider series from Today, and a compact collapsed disclosure lists every hidden series with occurrence count and **Undo** without mutating Calendar;
- a refresh shows the new `as_of` time and rejects late responses from older generations;
- sidebar/menu badges do not count low-confidence or ambiguous items as confirmed obligations, and uncertain cards do not render verified `P0`/`P1` badges;
- notification text contains counts/status by default, not mail or calendar contents.

No separate Work, Goals, Approvals, or Chief Of Staff destination ships before shadow-mode evidence shows that Today cannot support the workflow.

## Evaluation And Promotion Gates

Shadow mode produces predictions, not labels. Evaluation MUST include human labels for surfaced items and stratified samples of suppressed sources.

The local labeled corpus remains private and outside git. It includes:

- chronological Calendar changes including recurrence and timezone cases;
- chronological Gmail threads across human, bulk, transactional, and marketing classes;
- low/median/high-volume days;
- threads/events with updates, cancellation, moved deadlines, completion, and no operational content;
- source-local and cross-source examples of replied-but-not-fulfilled, fulfilled, delegated, being-handled, and unanswered work;
- positive, negative, ambiguous, confirmed, rejected, and retracted episode relations;
- owned, shared, adjacent, out-of-area, and configured high-consequence exception examples;
- meeting-preparation examples with both relevant and tempting-but-unsupported context;
- held-out dates not used to tune prompts or rules.

The versioned V1 fixture schema is strict and supports either `classification=private` or `classification=synthetic`. Private fixture files are owner-only and remain outside git; synthetic fixtures may be checked in. Each chronological case separately labels source class and day volume, item admission, item kind, lifecycle state, handled verdict, priority, high-consequence status, human confirmation, owner, responsibility, due date, evidence IDs, sensitivity, focus/overflow placement, coverage, authoritative-object need/state, Calendar change class, and evidence-route requirements. Relation and meeting-claim truth use separate label records. Predictions live in a separately validated shadow-run artifact bound to the exact fixture and policy version; generated output never becomes truth merely by being recorded.

The evaluator produces detection, source-class, source-date, evidence, ownership/responsibility, handled-verdict, focus/overflow, duplicate/stale/resurrection, relation, Calendar replay, meeting-claim, coverage, and budget metrics. It raises a hard stop rather than averaging away any external mutation, scope/privacy violation, nondeterministic recurring identity or cancellation/reschedule result, hidden critical/high item, high-consequence false handled result, unsafe reply/view/notification-only suppression, false human-confirmed closure, wrong-person assignment, missing high-consequence evidence, awareness padding, silent incomplete coverage, invalid required evidence route, activated false/ambiguous episode merge, unsupported meeting fact, or undisclosed budget overflow.

### Required Metrics

Detection:

- item precision and recall;
- critical/high-priority recall;
- false alarms per briefing;
- suppressed-source miss rate;
- classification by source class;
- marketing-to-visible leakage and recruiter-attention classification accuracy;
- owner/responsibility attribution accuracy under the active policy version.

Reconciliation:

- duplicate-active-item rate;
- false-merge and false-split rate;
- false-link, missed-link, and relation-retraction rate by episode relation type;
- stale-active-item rate;
- reschedule/cancellation/closure accuracy;
- resolved-item resurrection rate;
- time from source update to canonical item update;
- human correction and dismissal rate.

Handled-state verification:

- verdict precision/recall by `needs_action|responded_waiting|being_handled|fulfilled|unknown`;
- false-handled rate, especially fulfilled/being-handled verdicts that suppress required action;
- replied-but-not-fulfilled confusion rate;
- authoritative-source verification and cross-source satisfaction accuracy;
- time from satisfying evidence to handled-verdict update;
- incomplete-coverage cases incorrectly reported as handled or all-clear.

Briefing:

- high-priority recall in the appropriate section;
- focus precision@5 and recall of unresolved critical/high-priority episodes across focus plus urgent overflow;
- focus padding count and urgent-overflow disclosure accuracy;
- stale or terminal items shown as active;
- ranking usefulness from operator feedback;
- daily item churn and repeated false alarms;
- incomplete-coverage disclosure accuracy;
- hidden recurring-series leakage, disclosure, and restore accuracy;
- local/provider evidence-link validity and correct account/provider routing.

Meeting preparation:

- factual-claim evidence coverage and unsupported-claim rate;
- current commitment/decision/change recall;
- stale or wrong-person context rate;
- precomposition timeliness, revision-cache invalidation, and suppressed-series exclusion;
- primary-brief readability plus source/diagnostic appendix placement;
- per-source freshness/coverage disclosure accuracy;
- operator correction and usefulness feedback.

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
- high-consequence false-handled rate of zero and overall false-handled rate at most `0.01`;
- `100%` of unresolved critical/high-priority episodes represented in focus or urgent overflow on the labeled release set;
- zero awareness-padding items in focus;
- zero incomplete-coverage cases presented as verified handled or all-clear;
- `100%` validity for rendered local/provider evidence routes in the release fixture;
- duplicate-active-item rate at most `0.05`;
- stale-active-item rate at most `0.05`;
- resolved-item resurrection rate at most `0.01`;
- all suppressed categories represented in manual labels;
- owner-approved daily token/call budget with no silent overflow.

At least 30 chronological days, including representative high-volume days, are required before a Gmail shadow result may be promoted. A metric regression disables Gmail-derived briefing items without disabling retrieval indexing or Calendar.

Cross-source episode aggregation and handled-state suppression remain disabled until held-out chronological replay also meets the false-link, missed-link, retraction, authoritative-source, and false-handled gates. Local meeting-preparation precomposition may run in shadow because it reads only retained evidence and creates only a revision-bound derived cache; promotion as trusted preparation remains pending until every factual claim is evidence-linked, unsupported claims are zero on the release fixture, and stale/partial source coverage is always disclosed.

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
- `config/local/operations.yaml` is private, non-secret, schema-validated, and excluded from source control; it never stores provider tokens.
- Each connector requests the narrowest approved read scope; write scopes are absent before guarded-execution activation.
- Account IDs stored in SQLite are local non-secret identifiers.
- The private trial keeps a disposable owner-only raw API cache for 7 days and normalized evidence for 30 days; neither lane is knowledge authority.
- Attachment bytes are never fetched by the trial.
- Quoted Gmail reply history is stripped before normalized evidence is retained.
- Model payloads contain only the changed source material and bounded related-item context required for the decision.
- Provider use is explicit per operational detector role; unconfigured roles skip visibly.
- Raw model prompts/responses are not retained by default. Debug retention is opt-in, private, bounded, and reported in storage inventory.
- Briefing caches contain item IDs/ranking metadata rather than copied source bodies and expire after 30 days by default.
- Provider-native routes are constructed only from allowlisted adapter templates and stable IDs; arbitrary source URLs are not trusted navigation.
- Meeting-preparation caches follow briefing retention and never persist complete message, event, ticket, transcript, or document bodies.
- Source forgetting/redaction must invalidate or redact linked operational evidence and may leave a tombstoned item event explaining the loss of evidence.
- No private briefing content appears in notifications by default.
- No analytics leave the machine.

## Failure Semantics

- A connector failure marks its coverage stale/failed and does not abort other sources.
- Provider timeout or malformed detector output produces no transition and cannot resolve existing work.
- An unavailable authoritative provider leaves action satisfaction `unknown`; a notification cannot substitute for current object state.
- Failure or staleness in any authorized source that could satisfy an action prevents a verified cross-source all-clear.
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
- Inferring operator identity, responsibility, or completion from display names, traffic volume, read state, or a reply alone.
- Running the fact extractor, critic, resolver, or gardener over all mail for operational detection.
- Mandatory full-source rescans or one unbounded multi-source model context for each briefing.
- Padding an adaptive focus set with informational material or hiding urgent overflow behind a five-item cap.
- Treating a notification, copied summary, or agent-session summary as authority for an upstream object.
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
4. a validated local operations policy controls stable identity, responsibility ranking, configured sources, and exception handling with replayable version provenance;
5. every enabled adapter satisfies the source identity, cursor, budget, authoritative-signal, evidence-route, and coverage contract;
6. Today generates a freshness-stamped, storage-bounded briefing with evidence, confidence, reasons, handled verdicts, connector coverage, and true section total/included/omitted counts;
7. focus contains at most five confirmed action-bearing episodes, never pads with awareness, discloses all urgent overflow, assigns each item to one primary visible section, and keeps unverified items out of action candidacy and verified priority styling;
8. local and provider-native evidence routes are account-correct, allowlisted, and invalidated when evidence is forgotten;
9. meeting preparation is proactively cached inside the bounded window, opens as a human-readable source-linked brief, keeps evidence/diagnostics in an end appendix, invalidates on source revision change, and cannot mutate canonical state;
10. local completion, dismissal, snooze, correction, recurring-series hiding, and recurring-series restore survive restart/replay and prevent same-version resurrection or provider mutation;
11. knowledge facts, curation Queue, and `cos_*` compatibility paths remain behaviorally unchanged;
12. Calendar deterministic replay gates pass independently; private Gmail shadow access does not imply Calendar or Gmail promotion;
13. Gmail retrieval, knowledge, and operational lanes have separate admission and cost reporting;
14. Gmail shadow evaluation includes labeled suppressed mail and passes detection, handled-state, duplicate, staleness, reconciliation, focus, evidence-link, and budget gates;
15. cross-source episode relations are explicit and reversible, and no reply, notification, or incomplete coverage incorrectly suppresses a required action;
16. an operational provider failure cannot complete or cancel an existing item;
17. operational DB lock/failure cannot corrupt or block the knowledge DB;
18. backup/restore preserves both current item state and human feedback history;
19. no external mutation scope or action is enabled by the read-only/shadow implementation;
20. any later write capability declares a reversibility class and passes payload-bound approval, drift, verification, audit, and recovery acceptance.

Primary release-verification surfaces:

```bash
uv run pytest tests/test_operational_db.py tests/test_operational_state.py \
  tests/test_google_sources.py tests/test_google_cache.py \
  tests/test_gmail_operations.py tests/test_shadow_trial.py \
  tests/test_operational_briefing.py tests/test_operational_today.py \
  tests/test_operational_suppressions.py tests/test_today_presentation.py \
  tests/test_daemon.py -q
uv run python -m pkm_brain.operational_replay --help
swift test --package-path app
scripts/build-app.sh
```

Latest local release verification completed on 2026-07-14 with Ruff and diff checks green, 772 Python tests passing, 28 Swift tests passing, and the signed local app bundle installed and serving healthy runtime fingerprint `ac389246`. The installed selection is `gpt-5.6-luna` at `high` reasoning through restricted Codex. `executive-brief-v2` proactive preparation, scheduler completion, and recurring `Family Time` hide/Undo behavior were verified. The latest provider run completed Calendar but stopped Gmail before fetch at the approved `1200/1200` API cap; the exact retained 200-thread page then passed an isolated production-code replay with detector v6, zero model calls, no visible marketing leak, and no observation conflict. The macOS XCTest UI runner timed out while enabling automation mode before any UI test executed, so visual UI acceptance remains open rather than being treated as a pass. Owner review and all empirical promotion gates also remain open; neither complete Gmail provider/cursor behavior nor model judgment quality is claimed.

The implementation plan owns release sequencing. This spec owns the behavior and promotion gates.
