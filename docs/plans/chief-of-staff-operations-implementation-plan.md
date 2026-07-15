# Chief-of-Staff Operations Implementation Plan

**Status:** Gmail's operational mirror and encrypted 90-day archive are release-verified and installed; owner review and promotion remain pending
**Last verified:** 2026-07-14 against the completed live archive, installed daemon tools, and full release suites
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
11. A schema-validated private `config/local/operations.yaml` supplies stable identities, responsibility/ranking policy, configured sources, goals pointers, and exception rules; responsibility demotes rather than destructively filters.
12. Every source uses one typed incremental adapter contract with explicit authority, evidence-link, scope, budget, and coverage semantics.
13. Handled state is a versioned derived assessment distinct from item lifecycle; reply/read state alone cannot resolve or suppress work.
14. Today shows an adaptive focus set of at most five action-bearing episodes, discloses urgent overflow, and never pads with awareness.
15. Cross-source episode links are explicit, evidence-backed, and reversible; an ambiguous link never destructively merges source history.
16. A persisted briefing is a bounded preview at or below 240 KiB under the immutable 256 KiB storage ceiling and preserves true total/included/omitted counts for every section.
17. Each operational item appears in one primary visible section; low-confidence, provisional, or ambiguous items cannot become action candidates or render verified `P0`/`P1` badges.
18. There is no monolithic Chief-of-Staff model: deterministic reconciliation, selection, ranking, suppression, and cache validity remain authoritative; the shared Chief-of-Staff generative default is `gpt-5.6-luna` at `high` reasoning, and the only current generative role is the restricted, tool-less Gmail detector that inherits those defaults unless explicitly overridden.
19. Meeting briefs are proactively prepared from retained local evidence for timed, non-transparent events inside a 72-hour window after each Calendar shadow run and every 15 minutes; all-day, transparent, and high-precision `Family time` personal blocks remain visible and on-demand. Calendar refresh remains manual; Gmail provider mirroring is independently scheduled.
20. A meeting brief leads with human-readable context and preparation, keeps source-backed links in the body, and moves raw claims, evidence IDs, coverage, and retrieval diagnostics to a collapsed appendix.
21. Recurring Calendar-series hiding is a reversible local projection preference with a visible hidden-series disclosure and Undo; it never writes to Calendar or deletes operational evidence.
22. Marketing updates are hidden/audit-only, while routine individual recruiter activity is Attention unless exact evidence establishes a stronger commitment, deadline, or scheduled time.
23. Gmail transport uses a bounded owner-authorized API mirror: exact seven-day bootstrap, history-based incremental sync about every 600 seconds, atomic provider checkpoint plus durable local analysis queue, and no dependency on Luna for mailbox freshness.
24. Gmail history is copied into a separate AES-256-GCM encrypted local archive: one 90-day backfill followed by Gmail history incrementals in one state machine.
25. V1 archive search decrypts and scans locally. Agents can use only bounded daemon-backed `search_mail` and `get_mail_thread`; archive content never enters an LLM, operational detection, or fact extraction automatically.
26. Gmail deletion retains the local ciphertext. The archive never mutates Gmail.

## Program Sequence

| Phase | Outcome | External writes | State |
|---|---|---:|---|
| COS-0 | verified knowledge-layer foundation | none | complete |
| COS-1 | canonical boundary, local policy, adapter, privacy, eval, and rollout contracts | none | complete |
| COS-2 | separate operational kernel and deterministic lifecycle | none | complete |
| COS-3 | read-only Calendar evidence and reconciliation | none | latest live provider path complete; human review and promotion pending |
| COS-4 | coverage-aware Today focus, feedback, evidence audit, and proactive meeting preparation | none | release-verified and installed; visual UX review and empirical promotion pending |
| COS-5 | durable Gmail operational mirror, one-stage detection, and source-local satisfaction | none | mirror/provider-sync release-verified and installed; fresh live checkpoint/resync validation, human quality review, and promotion pending |
| COS-5A | encrypted 90-day Gmail history archive and bounded local evidence tools | none | release-verified and installed; owner review pending |
| COS-6 | reversible cross-source episodes, satisfaction, and production briefing gates | none | gated |
| COS-7 | local draft/action plans with guarded approval protocol | none | gated |
| COS-8 | capability-by-capability external execution | explicit only | gated |

Calendar and the operational kernel preceded Gmail implementation. The owner has separately approved Gmail read-only access for a private operational shadow trial, but that does not promote either source or enable Gmail retrieval/knowledge ingestion. Source-local detection and satisfaction must work before cross-source aggregation. Reconciliation must work before the briefing is considered trustworthy. Optional adapters follow the same contract and cannot bypass Calendar/Gmail gates. Drafting must work before execution.

Local release verification completed on 2026-07-14: Ruff and diff checks were green, all 873 Python tests and 28 Swift tests passed, and the signed local app bundle built, installed, launched, and served healthy runtime fingerprint `0.1.6-12a45456-7adaae5c`. The installed restricted-Codex selection is `gpt-5.6-luna` at `high` reasoning. The `gmail_mirror_sync` and `gmail_archive_sync` jobs both completed after installed startup; the `executive-brief-v2` packet was previously verified as prepared in advance, and recurring `Family Time` hide/Undo behavior passed a live check.

The live `gmail_mirror_sync` checkpoint is complete in incremental mode, and the separate encrypted archive completed its fixed 90-day copy plus a five-change history catch-up. The archive holds 7,746 active messages across 6,857 threads in an encrypted database occupying about 403 MB at installed verification; its integrity, permissions, identity binding, Keychain round trip, bounded installed `search_mail`/`get_mail_thread` responses, untrusted-content labeling, and attachment-byte exclusion all passed. Separately, the retained-page operational replay still provides the deterministic local-routing evidence: marketing and bulk suppression, recruiter-to-Attention routing, and immutable observations. These results establish mailbox transport and archive correctness, not detector judgment quality or daily briefing trust. Native visual automation remains host-blocked before test execution, while owner visual/content review, continued labeling, and every empirical promotion gate remain next.

## 2026-07-14 Operator-Feedback Tranche

This tranche improves the owner-facing loop without widening connector scopes or enabling provider writes.

Implementation status:

- [x] Document the split model architecture and keep deterministic selection authoritative.
- [x] Set the shared Chief-of-Staff default to `gpt-5.6-luna` at `high` reasoning and make the restricted, tool-less Gmail detector inherit it unless an explicit Gmail-specific config/environment override is present; retain effective-model/config-source/prompt/version/usage audit fields.
- [x] Turn the meeting packet into a human-readable executive brief with source-backed context, conservative event-to-background relevance gating, open questions, preparation prompts, and relevant links.
- [x] Move raw event/fact claims, evidence IDs, coverage, freshness, and retrieval diagnostics into a collapsed end appendix.
- [x] Add revision-bound prepared-packet persistence, a 72-hour timed/non-transparent eligibility window, after-Calendar-run precomposition, and a daemon-local 15-minute job.
- [x] Show **Open brief** for a prepared current revision and retain **Prepare now** as the fallback.
- [x] Add durable per-account recurring-series suppression, exclude matching occurrences from Today and preparation, show a compact hidden-series disclosure, and support Undo.
- [x] Keep suppression entirely local; do not dismiss source items, delete evidence, or mutate Calendar.
- [x] Suppress marketing campaigns before semantic detection, keep them in the collapsed audit only, and prevent legacy marketing items from leaking into Uncertain.
- [x] Classify individual recruiter outreach as Attention by default while preserving stronger evidenced commitments, deadlines, and scheduled events.
- [x] Re-run the full Python, Swift, Ruff, diff, signed-build/install, and daemon-health gates against the completed tranche.
- [x] Verify the installed `gpt-5.6-luna`/`high` restricted-Codex selection, proactive `executive-brief-v2` scheduler result, and recurring-series hide/Undo path.
- [x] Replay detector v6 through production code against the exact retained 200-thread page without bypassing the exhausted live API budget; confirm deterministic routing, derived revisions, and no marketing leak or observation conflict.
- [ ] Complete visual UI acceptance of the installed tranche with the owner.
- [ ] Review a fresh private Calendar/Gmail result with the owner; this is evaluation, not automatic promotion.
- [ ] Complete the labeled empirical promotion gates; release verification does not waive them.

Focused automated coverage is required for:

- operational migration and clean upgrade creation of `ops_suppression_rules` and `ops_meeting_packets`;
- recurring-series hide/list/filter/restore behavior, one-off rejection, restart durability, and zero provider mutation;
- prepared-packet save/load bounds, exact-revision invalidation, 72-hour timed/non-transparent selection, hidden-series exclusion, expiry pruning, and scheduler failure isolation;
- meeting brief decoding and the separation between primary human-readable sections and the source/diagnostic appendix;
- source link preservation, unsupported-claim prevention, partial-coverage warnings, and fallback preparation;
- marketing campaign suppression, marketing-boilerplate false urgency, legacy visible-item removal, recruiter Attention, and stronger recruiter obligation preservation;
- deterministic focus/section invariants and bounded briefing serialization after filtering.

Verification checklist:

```bash
uv run pytest tests/test_operational_suppressions.py \
  tests/test_operational_briefing.py tests/test_gmail_operations.py \
  tests/test_shadow_trial.py tests/test_daemon.py -q
uv run ruff check src tests
uv run pytest -q
swift test --package-path app
scripts/build-app.sh
scripts/install-app.sh
scripts/ui-acceptance.sh
```

## COS-0 - Knowledge Foundation Baseline

Completed by commit `3937316`:

- knowledge schema 21 and current fact/entity/wiki behavior preserved;
- auth-only Gmail and Slack connector shells;
- isolated Gmail benchmark with reconciled token accounting;
- 510 Python tests, 17 Swift tests, Ruff, and the full macOS UI acceptance path green;
- Gmail knowledge capture and durable-fact ingestion remain disabled; later operational shadow access is a separate lane and grant.

The historical `audit-remediation-2026-07-12` branch is not merged because its semantic fixes were replayed into the newer main history.

## COS-1 - Boundary And Contracts

### Deliverables

- canonical operational spec and this plan;
- updated product, capture, curation, app, retrieval, sync, README, and compatibility pointers;
- explicit Calendar and Gmail privacy/scope/retention contracts;
- explicit local operations-policy schema covering stable identities, responsibility areas, configured sources, ranking/exception rules, timezone, and goals pointers;
- common source-adapter contract covering provider authority, replay, evidence routes, budgets, and coverage;
- handled-state, authoritative-source precedence, adaptive-focus, cross-source-relation, and derived-meeting-preparation contracts;
- a versioned operational eval fixture format;
- an all-or-nothing Knowledge Curation rename decision.

The private eval fixture format includes lifecycle truth separately from handled-state truth, source coverage, policy version, authoritative-object state, positive/negative episode links, focus/overflow expectations, and evidence-route expectations. Meeting-preparation fixtures label every supported and unsupported factual claim.

### Naming decision

Conceptual documentation changes immediately from "Chief-of-Staff curation" to "Knowledge Curation." Physical `cos_*` modules, tables, CLI, API/config fields, migrations, fixtures, and tests remain unchanged through COS-4. After the Calendar shadow vertical slice is accepted, one dedicated tranche may rename the entire surface to `curation_*`, with compatibility aliases and a dated removal horizon. A partial rename is prohibited.

### Exit gate

Every new operational table, job, adapter, API, and UI control has exactly one owning spec. No document describes `cos_actions` as the operational action ledger. The local operations policy and every enabled adapter validate against a versioned schema, and evaluation can distinguish item-state, handled-state, episode-link, focus-selection, and evidence-link errors.

The initial architecture and privacy contracts were completed by commit `a44b713`; the operator-policy, adapter, handled-state, focus, meeting-preparation, and episode-relation amendment was completed by commit `005ff63`. The implementation foundation now includes a strict owner-only `operations.yaml` V1 loader plus a versioned private/synthetic fixture loader, prediction binding, metrics, held-out promotion gates, and non-averagable hard-stop evaluation. Calendar/Gmail adapters must consume this boundary in their owning phases; they may not reimplement or weaken it.

## COS-2 - Operational Kernel

### Persistence

Create an independently migrated `db/ops.sqlite` with:

- `ops_schema_migrations`;
- `ops_observations` for immutable normalized source revisions;
- `ops_items` for canonical current state;
- `ops_item_events` for append-only transitions and feedback;
- `ops_source_cursors` for replay-safe connector progress and source coverage.

The operator-feedback migration also adds two bounded supporting tables without changing the one-item aggregate:

- `ops_suppression_rules` stores reversible, account-bound Calendar recurring-series projection preferences;
- `ops_meeting_packets` stores revision-bound derived meeting briefs capped at 256 KiB with explicit generation and expiry times.

Common item fields remain columns: source-unit/object identity, kind, state, title, owner/counterparty metadata, starts/due/ends/expires/snooze times, priority, confidence, current observation, reconciliation method, human-action provenance, and timestamps. Provider/type-specific material stays in validated JSON until repeated usage proves a column is necessary.

Initial item kinds are `event`, `commitment`, `waiting`, `follow_up`, `deadline`, and `attention`. The universal state enum is `active`, `resolved`, `dismissed`, `cancelled`, or `expired`. Kind-specific semantics are expressed through deterministic transition validation rather than separate tables.

Handled verdicts are derived assessments and do not extend this state enum. Cross-source episode relations are deferred to a reviewed COS-6 migration; COS-2 does not pre-commit a relation schema or weaken the one-item aggregate.

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
- `operational_service.py`
- `recovery.py`
- `paths.py` for the operational DB path

### Current implementation

The completed COS-2 foundation consists of:

- independently migrated `ops.sqlite` tables and an explicit bootstrap path;
- immutable bounded observations, canonical items, append-only hashed events, and replay-safe source cursors with generation compare-and-swap; Gmail derived interpretation revisions are split from raw provider revisions, and optional `policy_version` observation metadata preserves compatibility with legacy rows;
- exact source-unit binding, strict UTC normalization for present timestamps, provider-authority reconciliation, stale/equal-authority protection, lifecycle feedback, and one atomic source-unit/cursor batch primitive;
- owner-only database/WAL/SHM handling, bounded lock retry, and focused isolation/concurrency tests;
- a daemon-owned operational service that revalidates role, home, node identity, restore quarantine, and the active daemon lease for every mutation, with in-process and cross-process serialization;
- fail-closed handling for secondary, malformed, missing-after-configuration, mismatched, replaced-lock, and restored-home authority states;
- one owner-only, checksummed `database_pair` recovery generation created under a fixed SQLite write barrier, with exact schema/integrity validation and a durable completion marker;
- isolated restore that binds copied database content to the recovery manifest, preserves knowledge review plus operational feedback/cursors, and remains quarantined until a future explicit topology activation workflow.

The daemon now owns and wires one fenced `OperationalService` into the manual shadow controller and Today presentation service. After the owner has approved a valid policy and exact Gmail grant, the scheduled provider lane may initialize `ops.sqlite` locally without requiring a first Shadow click; ordinary knowledge initialization and connector capture still do not mutate it. The separate `provider_sync` lane maintains the rebuildable Gmail mirror without writing `ops_items`; Calendar/Gmail reconciliation, handled assessments, briefing snapshots, item feedback, suppression preferences, prepared packets, and missing reports still use the operational service. One 15-minute local job prepares revision-bound meeting briefs from retained evidence and never polls a provider.

### Verification

- fresh-store and same-version re-initialization idempotence; add a populated upgrade fixture before the first post-v1 migration;
- WAL, busy timeout, foreign keys, and short transaction behavior;
- observation replay idempotence;
- update/reschedule/cancel/dismiss/restore history;
- recurring-series suppression/list/restore durability and one-off-event rejection;
- meeting-packet size, revision, expiry, idempotence, and eligibility-window behavior;
- concurrent short-writer coverage;
- a test proving no knowledge table changes;
- owner-only DB/WAL/SHM permissions and explicit missing-store failure;
- primary/single-role writer fencing at the service boundary;
- a coordinated backup/integrity fixture covering both SQLite databases.

### Exit gate

A deterministic fixture replay can create, update, reschedule, cancel, resolve, and dismiss one item without duplication or any `brain.sqlite` mutation. The daemon/service rejects secondary writes, and a coordinated backup/restore fixture preserves the item and its human feedback.

This gate passes in the COS-2 working tree. Cross-process lease contention, daemon-close quiescence, topology fail-closed cases, matched WAL-backed recovery, tamper/mixed-generation rejection, manifest-bound isolated restore, and restored-home quarantine are covered explicitly.

## COS-3 - Read-Only Calendar

### Connector contract

Add a separate Google Calendar account grant with identity plus `https://www.googleapis.com/auth/calendar.events.owned.readonly`. The initial application policy reads only the owned primary calendar; additional owned calendars require an explicit ID allowlist, and shared calendars require a separate decision about the broader read scope. Do not widen the Gmail credential. Credentials remain in Keychain; local config contains only non-secret account/status/cursor metadata.

Implement the versioned `config/local/operations.yaml` loader before enabling the connector. The Calendar slice consumes only validated operator identity, timezone, briefing/preparation window, primary/allowlisted calendar IDs, policy version, and provider host/account routing. Missing or invalid required policy makes the adapter unavailable; it never falls back to inferred identity or an unrestricted calendar scan.

Calendar is also the reference implementation of the common adapter interface: typed source-unit batch, atomic cursor progression, bounded initial/resync windows, explicit coverage, retry/rate-limit budget, evidence references, and an allowlisted provider-route builder. The projection service receives normalized observations, never raw provider objects.

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

An opted-in Calendar shadow run produces no LLM calls and no external mutation, while recurrence exceptions, moves, cancellations, timezones, policy validation/versioning, local/provider evidence routing, coverage, and replay pass labeled fixtures. Invalid policy or an unvalidated provider route fails visibly without widening access.

### Current implementation

The manual runner now uses a separate exact-scope Calendar grant, reads only `primary` with recurring instances expanded, includes deletions, preserves recurrence/exception identity, and applies observations plus cursor progress atomically. It uses a bounded 14-day past/90-day future initial window, persists resumable page/sync checkpoints, enforces a durable daily request budget on every provider attempt, retains revision-addressed evidence, and reports incomplete coverage rather than advancing past failed work. Local release verification is complete, and the repaired owner-authorized validation reported Calendar complete and persisted its coverage. Human review, replay labels, and Calendar promotion evidence remain pending.

## COS-4 - Today Shadow Briefing

Extend `/api/digest` with an optional briefing projection and add a narrow feedback endpoint. Do not add a navigation destination.

Today renders:

1. source coverage/freshness;
2. an adaptive focus set of zero to five action-bearing episodes;
3. urgent overflow, never hidden behind the focus cap;
4. now and today;
5. upcoming and changed;
6. needs correction or uncertain;
7. separate awareness material;
8. the existing system pulse below the briefing.

Every item supports evidence inspection plus correct, done, snooze, dismiss, restore, and report-missing feedback where valid. Feedback appends an operational item event; it does not masquerade as a provider observation and never mutates a fact merely because the user changes an item.

A recurring Calendar event additionally supports **Hide recurring series**. The rule is keyed by account and provider series ID, filters every retained/future occurrence from Today and meeting preparation, and appears in a compact hidden-series disclosure with **Undo**. This is a local presentation preference; it does not change item lifecycle or Calendar.

### Briefing requirements

- deterministic ranking with injected clock/timezone and validated operations-policy version;
- no cancelled/resolved/dismissed item in active sections;
- no duplicate mirrored event in the top projection;
- no awareness padding in focus and no suppressed critical/high-priority overflow;
- `unknown` handled state when relevant evidence or source coverage cannot support suppression;
- every card has a stable local evidence route and only validated account-correct provider routes;
- explicit `Calendar only`, `Gmail unavailable`, `stale`, and `incomplete` coverage labels;
- a serialized section preview at or below 240 KiB under the immutable 256 KiB storage ceiling, with true total/included/omitted counts preserved when cards are omitted;
- exactly one primary visible item section per operational item, while independent facet counts remain available for evaluation;
- no low-confidence, provisional, or ambiguous item in action candidacy and no verified `P0`/`P1` styling on uncertain cards;
- persisted briefing runs only when needed for shown/not-shown evaluation and feedback attribution.

Add a bounded human-readable executive brief for an operator-selected or eligible upcoming meeting. The initial packet may combine Calendar metadata, active operational items, local operations policy, and approved Brain retrieval/facts/pages. The primary view leads with purpose/context, relevant background, agenda/talking points, open questions/preparation, and relevant links. Each factual claim keeps evidence IDs, freshness, and source coverage in a collapsed end appendix; suggested talking points are visually non-factual. The packet is derived, stores no full source bodies, and cannot mutate an item or fact.

Precompose revision-current packets for unsuppressed active timed events that are not transparent and do not match the high-precision normalized `Family time` title/prefix in the next 72 hours after a Calendar shadow run and from one 15-minute daemon-local job. Excluded events retain their Today card, series controls, and explicit on-demand preparation without entering the proactive queue. Persist at most 256 KiB per packet, expire after 30 days, and treat a changed Calendar revision or packet-content version as a cache miss. User-visible retrieved background must share a meaningful normalized term/entity with the event title or retained Calendar notes after generic meeting words and operator identity terms are removed; non-matching retrieval stays diagnostics-only. The job uses retained local evidence only; Calendar polling and Gmail detector analysis remain manual-run behavior, while Gmail provider mirroring is scheduled separately. **Open brief** reads the current cache and **Prepare now** is the explicit fallback.

### Exit gate

A user can inspect and correct a Calendar-backed briefing for at least two weeks of shadow replay without duplicate or stale cards exceeding the configured gates. Focus never pads, urgent overflow disclosure and evidence-route validity are `100%` in fixtures, and meeting packets contain zero unsupported factual claims while partial coverage is always visible.

### Current implementation

Today now exposes **Run Shadow**, polls one background provider-reading run through accepted/running/terminal state, refreshes the briefing after every terminal result, and keeps Calendar and Gmail coverage visible even when the final projection path fails. A prominent outcome card distinguishes complete, partial, and failed results. The briefing assigns each operational item to one primary section, bounds persisted cards to the 240 KiB target, and keeps true total/included/omitted counts. The ignored/suppressed audit is collapsed by default with its true total and bounded preview; marketing updates stay there rather than entering Uncertain. Uncertain items cannot enter the action set or display verified priority badges. Cards can open retained local evidence. Local confirm, note-required correction, done, snooze, dismiss, restore, recurring-series hide/undo, and report-missing actions write only operational state.

The meeting packet now renders as an executive brief rather than raw retrieval output: readable context and preparation lead, validated Calendar/Brain/source links are available in the body, and coverage plus claim/evidence diagnostics are collapsed at the end. Revision-bound `executive-brief-v2` packets are precomposed after Calendar shadow work and by the local 15-minute scheduler for timed, non-transparent events in the next 72 hours; all-day and transparent events stay on-demand, prepared cards show **Open brief**, and **Prepare now** remains the fallback. Suppressed recurring series are excluded and summarized in a compact reversible disclosure. Calendar refresh is still manual; Gmail mailbox refresh is now independent. Release verification confirmed a packet prepared in advance, scheduler completion, and live `Family Time` hide/Undo behavior in the installed app. Owner visual UX review and empirical promotion remain pending.

## COS-5 - Gmail Operational Detection

This phase requires explicit approval of Gmail read-only scope, local retention, redaction, deletion, attachment, and quoted-history behavior.

### Three independent lanes

1. Retrieval indexing: approved normalized thread snapshots.
2. Durable knowledge: the conservative human/evidence fact pipeline.
3. Operational detection: high-recall provisional current-state operations.

Only lane 3 is enabled. Retrieval indexing and durable knowledge ingestion remain disabled and are not implicit side effects of operational evidence access.

### Provider mirror and analysis queue

Use the owner-authorized Gmail API rather than POP/IMAP or a mail-app database. The first bounded sync uses exactly `newer_than:7d -in:spam -in:trash`; successful completion records the Gmail `historyId`, after which `history.list` identifies changes and `threads.get` refreshes only affected threads. Register `gmail_mirror_sync` on startup and about every 600 seconds on its own `provider_sync` scheduler lane for `single|primary` roles. Every run requires a valid private policy, `sources.gmail.enabled=true`, `content_access_approved=true`, and a connected exact-account, exact-provider-subject, exact-scope identity plus `gmail.readonly` grant; after those gates pass, the daemon-owned job may initialize `ops.sqlite` itself.

Store sanitized operational evidence at `~/brain/cache/gmail-mirror/gmail-mirror.sqlite` inside a mode-`0700` directory, with owner-only SQLite/WAL/SHM files. This is a rebuildable cache protected by local OS permissions/volume security, not an application-encrypted authority. Retain immutable thread revisions, current pointers, provider-confirmed tombstones, a durable revision-bound triage queue, and resumable full/incremental checkpoints. Attachment metadata may remain, but attachment bodies/bytes are never fetched or stored.

One mirror transaction commits accepted revisions, current pointers, tombstones, queue insertions/supersessions, content-free quarantine records, and the checkpoint together. A malformed, oversized, or immutable-conflicting thread is isolated without rolling back valid peers or cursor progress, and its unresolved quarantine keeps Today partial. After the normal complete checkpoint commits, a separate generation may retry at most ten distinct due quarantined threads against the retained history cursor. Durable retry metadata supplies ten-minute-to-one-day exponential backoff, restart recovery, and immediate parser-version reprocessing; only acceptance or provider-confirmed deletion resolves an entry. Retry-only budget/provider/transport failure cannot roll back mailbox freshness and is reported as an analysis deferral. An expired history cursor starts a bounded full resync using the same query while preserving prior mirror state; an invalid saved incremental page token replays from the retained history cursor, while an invalid full-scan token takes a new baseline. Absence from the bounded listing never implies deletion. Luna leases and drains only local queue rows, makes no Gmail calls, and cannot delay or roll back mailbox checkpoint progress. Provider preflight covers the bounded page envelope and every configured transport attempt before thread GETs begin. Today reports provider freshness/resync independently from pending, leased, failed, deferred, quarantined, and oldest-age analysis backlog.

One changed thread is processed once, or in a failure-isolated batch of small transactional threads. Advertising, newsletter, promotion, and other marketing campaign classification gates broad keyword admission before semantic detection, while a changed thread with an already tracked item remains detector-eligible. Marketing updates are audit-only and cannot fall back into visible uncertainty. Individual recruiter outreach is carved out as Attention; bulk job alerts remain marketing, and exact commitments/deadlines/scheduled times preserve their stronger semantics. Input contains new messages, bounded thread context, source-native timestamps, and compact plausibly related active items. Output is a structured operation:

- ignore;
- create item;
- update/reschedule/cancel/close item;
- needs reconciliation.

No fact critic, fact route resolver, entity gardener, or wiki routing is invoked. The restricted, tool-less detector inherits the shared Chief-of-Staff default of `gpt-5.6-luna` at `high` reasoning unless an explicit Gmail-specific config/environment override is present. Structured validation is isolated per thread so one malformed result cannot discard valid siblings in the same batch; any affected admitted non-marketing thread becomes explicit uncertainty without promoting model output. Cost is measured rather than inferred; 100–150K tokens/day is a planning hypothesis, not a benchmark result.

Implement source-local action-satisfaction verification in the same phase. Stable configured operator email identities plus Gmail message/thread lineage determine authorship and response; display names do not. The derived verdict is `needs_action|responded_waiting|being_handled|fulfilled|unknown`, with supporting/contradicting evidence, source coverage, method/version, policy version, confidence, and `as_of`. Read/viewed state and an outgoing reply never prove fulfillment. A direct supplied/declined result may recommend an allowed lifecycle transition, but deterministic reconciliation applies it.

Messages that notify about a ticket, review, build, reservation, invoice, or other upstream object are leads. Without a fresh authorized adapter for that canonical object, their authoritative-state check remains `unknown`; email text cannot close the item. Provider-native Gmail routes are built from allowlisted account/thread/message IDs, not arbitrary message links.

### Label program

Shadow predictions are not labels. Build a chronological, versioned set containing:

- surfaced positives;
- stratified suppressed mail;
- full-day audits;
- missed-item reports;
- immutable holdout days;
- operational importance, type, state, evidence, due/owner, responsibility area, and sensitivity labels;
- source-local replied-but-not-fulfilled, fulfilled, delegated, unanswered, and ambiguous handled-state labels;
- focus, urgent-overflow, authoritative-object-needed, and evidence-route expectations under a declared policy version.

### Exit gate

Severity-weighted recall, false-alarm rate, source-date accuracy, handled-verdict accuracy, replied-but-not-fulfilled confusion, focus/overflow selection, evidence-route validity, schema-repair rate, and token/call budgets meet their approved thresholds on held-out replay. High-consequence false handled is zero, overall false handled is at most `0.01`, no incomplete authoritative-source coverage is presented as handled, and no focus slot is padded with awareness.

### Current implementation

The owner-approved private lane now has the scheduled provider mirror and durable local triage queue described above. Provider units remain bounded to 200 threads, pagination/checkpoints are resumable, MIME normalization strips quoted history, and sanitized storage excludes attachment bytes. Marketing-campaign gating precedes broad keyword matching, tracked threads remain detector-eligible, routine individual recruiter activity becomes normal-priority Attention unless stronger evidence exists, and malformed batch entries are isolated per thread. Only the restricted, tool-less Codex detector may receive bounded queued thread content; the installed selection is the shared `gpt-5.6-luna`/`high` default unless a Gmail-specific override is present. Detector `gmail-operations-v6` remains subordinate to deterministic evidence/lifecycle validation. Daily provider and detector budgets fail visibly into their respective mailbox or analysis coverage instead of conflating the two.

The installed mirror now has a complete live incremental checkpoint and resumed safely across daemon restarts. Expired-history recovery remains fixture-tested rather than deliberately forced against the live mailbox. The retained-page replay remains separate evidence for deterministic local routing and immutable-observation behavior, not model quality.

Focused release coverage must prove exact policy/grant/role gates; exact bootstrap query; dedicated scheduler lane/cadence/startup behavior; full and incremental pagination; crash-atomic mirror/queue/checkpoint commits; immutable revision/current-pointer and supersession behavior; provider-confirmed tombstones; expired-history resync that preserves old state and never deletes by absence; attachment-byte exclusion; concurrent sync serialization; queue lease/retry/restart behavior; bounded quarantine retry/backoff, parser-version bypass, schema-one upgrade, and retry-only failure isolation; Luna operation with zero Gmail calls; and separate mailbox-freshness versus analysis-backlog presentation. Run the focused mirror/sync/source/scheduler tests before the full Python, Ruff, Swift, signed-build/install, and owner live-review gates.

## COS-5A - Encrypted Gmail History Archive

The archive is a separate local SQLite store for exact Gmail `RAW` messages, including attachments, plus encrypted parsed text. A Keychain-backed key protects archive content with AES-256-GCM. It does not widen the sanitized operational mirror or write to Brain knowledge state.

The sync job uses one durable state machine:

1. copy the fixed previous 90 days with resumable pagination;
2. transition to Gmail history incrementals for new and changed messages;
3. restart the full scan safely when Gmail expires the history cursor;
4. retain local ciphertext when Gmail reports a deletion.

V1 search decrypts and scans locally, then returns bounded results. `search_mail` and `get_mail_thread` require the authenticated daemon, enforce response limits, return no attachment bytes, and treat all message content as untrusted. The direct MCP fallback does not expose them.

The scheduler reports the stored-message count, current state, or a plain-language pause reason; Gmail's unreliable result-size estimate is not presented as a total. Managed storage reports archive file size. Acceptance requires focused encryption/sync/tool tests, a completed private 90-day backfill, one observed incremental update, an expired-cursor restart test, provider-deletion retention, and confirmation that no Gmail mutation, LLM call, operational detection, or fact extraction occurs.

COS-5A passed its release gate on 2026-07-14. The fixed window copied 7,742 messages and the first history catch-up processed five changes, yielding 7,746 active messages across 6,857 threads. Original raw data totals 758,041,932 bytes; the encrypted database occupied about 403 MB at installed verification and passed `quick_check` with exactly three application tables, owner-only permissions, and a verified 32-byte Keychain key. Installed daemon calls returned bounded, untrusted search/thread payloads with provider links and no attachment bytes. Archive sync made no LLM, Knowledge, operational-detector, or Gmail-mutation call. Expired-cursor restart and provider-deletion retention remain covered by deterministic tests; owner content/UX review remains open.

## COS-6 - Cross-Source Reconciliation

Link Calendar and Gmail only after source-local identity is reliable.

Deterministic candidate keys precede semantic candidates:

1. provider object/thread/message lineage;
2. explicit business identifier or link;
3. existing item/observation lineage;
4. participants + normalized subject + temporal proximity;
5. bounded semantic similarity as a candidate generator only.

Ambiguous matches remain separate or require confirmation. False merge is treated as more severe than a temporary duplicate.

### Episode relations and handled state

Add a dedicated reviewed migration for reversible episode relations only after fixture shape and lifecycle are approved. A relation records exact endpoints, `same_episode|duplicate_of|responds_to|fulfills|delegates|supersedes` type, supporting evidence, method/version, policy version, confidence, creator, and proposed/confirmed/rejected/retracted status. Relation changes append audit events and never delete or rewrite endpoint observations/items. A human rejection blocks recreation from the same evidence/version; retraction recomputes handled and briefing projections.

Cross-source handled assessment evaluates the complete authorized source set that could satisfy an obligation. An email response may be satisfied in collaboration, a work tracker, or a code-host object only when an evidence-backed relation and fresh authoritative state prove it. Missing/stale authoritative coverage yields `unknown`, not suppression. One focus card may aggregate a confirmed episode while preserving every source-local item and link.

Enrich meeting preparation with confirmed episode relations, fresh communication evidence, and authoritative work-object changes. Full source bodies remain in their governed evidence stores; packets retain bounded derived text and evidence IDs.

### Optional adapter tranches

After the Calendar/Gmail COS-6 gate passes, add read-only adapters independently rather than as one multi-source release:

1. local Git worktree state;
2. local agent-session outcomes, unfinished tasks, and explicit blockers;
3. approved code-host review/CI/merge state;
4. approved collaboration direct-ask and stable-user-ID thread progression;
5. approved work-tracker assignment, question, due-date, and canonical status history.

Each tranche implements the common adapter interface, adds private labeled replay fixtures, declares scope/retention/budget/authority rules, and can be disabled without disabling another source. Local Git and agent summaries cannot establish external completion. Code-host and work-tracker state outrank their notification copies. Each remote adapter requires a separate authorization decision and implementation commit.

### Required metrics

- duplicate-active and stale-active rate;
- false-merge and false-split rate;
- false-link, missed-link, relation rejection/retraction, and wrong-relation-type rate;
- update/reschedule/cancel/closure recall;
- premature-close rate;
- handled-verdict precision/recall and false-handled rate by source combination;
- replied-but-not-fulfilled and authoritative-source-verification accuracy;
- resolved-item resurrection;
- wrong person/project/episode linkage;
- high-severity miss rate and detection latency;
- replay idempotence;
- focus precision@5, urgent-overflow recall, padding count, and stale/duplicate share;
- local/provider evidence-route validity;
- meeting-packet evidence coverage, unsupported-claim rate, and stale/wrong-person rate.

### Exit gate

The production cross-source briefing remains disabled until chronological replay meets relation, authoritative-source, handled-state, focus/overflow, evidence-link, and meeting-preparation gates, not merely detection precision. High-consequence false handled, hidden critical/high-priority overflow, invalid evidence routes, and unsupported meeting-packet facts are zero in the release fixture; overall false handled and false link are each at most `0.01`.

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

- source/authoritative-object coverage, replay window, adapter versions, and operations-policy version;
- calls, total/uncached tokens, latency, and invalid-output rate;
- item precision/recall by kind and severity;
- reconciliation, reversible episode-relation, and handled-verdict metrics;
- focus precision@5, critical/high-priority overflow recall, awareness-padding count, and evidence-route validity;
- marketing-to-visible leakage, recruiter-attention classification, and suppressed-source miss rate;
- recurring-series suppression/restore leakage and disclosure accuracy;
- meeting-preparation factual evidence coverage, unsupported-claim rate, stale/wrong-person rate, precomposition timeliness, and revision invalidation;
- archive stored-message progress, file size, incremental freshness, pause reasons, and bounded-tool failures;
- corrections, dismissals, snoozes, and reported misses;
- DB size, write latency, lock errors, backup/integrity result;
- model/prompt/classifier versions.

No release can average away a wrong-person link, false closure of a user-confirmed commitment, false handled assessment that suppresses high-consequence work, hidden urgent overflow, unsupported factual meeting-preparation claim, missed high-consequence cancellation, or unapproved external write.

## Backup, Sync, And Recovery

- `ops.sqlite` uses SQLite backup semantics, never a live file copy.
- Backup manifests bind the knowledge and operational snapshots taken under one daemon-controlled recovery point.
- Operational state is primary-only initially and is never live-rsynced.
- The Gmail archive is primary-local encrypted evidence and is not copied by Brain source sync.
- A secondary can render explicitly stale replicated briefing state only after coordinated snapshot support lands.
- Immutable provider evidence allows deterministic rebuild of derived observations/items, but user corrections and approvals are canonical and must be backed up.
- Fact promotion is an asynchronous replay-safe handoff into the normal Knowledge Curation action path.

## Stop Conditions

Stop the phase and preserve the previous release when:

- an operational write touches `brain.sqlite` directly;
- a knowledge action is used to represent a current item or external side effect;
- Calendar recurrence/timezone/cancellation replay is not deterministic;
- source coverage is incomplete but Today presents an all-clear;
- briefing serialization exceeds the 240 KiB projection target, omits cards without true total/included/omitted counts, or a terminal projection error erases completed source coverage/usage;
- an unavailable authoritative object is treated as handled from notification text;
- a reply, view marker, display name, or unconfirmed cross-source relation suppresses required action;
- focus is padded with awareness or omits undisclosed critical/high-priority overflow;
- an operational item is repeated across primary sections, or an uncertain/unverified item is promoted into action candidacy or verified `P0`/`P1` presentation;
- a marketing update appears in a visible section, a bulk job alert is promoted as personal recruiter activity, or routine individual recruiter activity disappears instead of entering Attention;
- a recurring-series rule changes provider state, hides a different account/series, lacks an Undo disclosure, or survives explicit restore in the Today projection;
- a provider link fails its scheme/host/tenant/account validation or a forgotten evidence route remains active;
- a meeting packet emits an unsupported factual claim, presents diagnostics as the primary brief, hides stale/partial coverage, uses a stale Calendar revision, or prepares a locally suppressed series;
- duplicate, stale, false-merge, false-link, false-handled, premature-close, or high-severity-miss gates regress;
- calls/tokens grow without a per-changed-source budget;
- archive plaintext appears outside the encrypted store or an archive operation invokes an LLM, fact extraction, or Gmail mutation;
- archive sync cannot resume/restart safely, provider deletion removes local ciphertext, or mail tools bypass the authenticated daemon and bounds;
- SQLite lock/integrity or coordinated-backup tests fail;
- an external plan lacks exact approval binding or a reversibility class;
- planner and approver share the same authority surface.

## Commit And Rollout Discipline

Use separate commits for:

1. knowledge foundation (`3937316`);
2. initial operational specs/plan (`a44b713`);
3. operational DB/kernel (`03be261`);
4. local-policy, adapter, handled-state, focus, evidence-link, meeting-preparation, and episode-relation contract amendment (`005ff63`);
5. COS-2 writer fencing plus coordinated backup/restore;
6. strict operations-policy loader plus private/synthetic shadow-evaluation schema and hard-stop scorer;
7. Calendar adapter plus safe route builder;
8. Today focus/API/UI plus Calendar/Brain meeting preparation;
9. Gmail retrieval/detection plus source-local satisfaction verification;
10. operator-feedback tranche: proactive revision-bound meeting briefs, recurring Calendar-series suppression/undo, marketing hiding, recruiter Attention, and model-default inheritance;
11. encrypted Gmail archive, scheduler progress, and bounded daemon mail tools;
12. reversible cross-source episode relations, satisfaction, and briefing gates;
13. each optional Git, agent-session, code-host, collaboration, or work-tracker adapter as its own read-only commit;
14. draft/execution capabilities, split again by capability and reversibility class.

Contract commits do not smuggle in runtime behavior. Every implementation commit updates the owning phase status and includes focused tests, private fixture/eval results where applicable, plus the full no-regression gate appropriate to its blast radius. A phase commit is pushed only after its owning gate passes; a later adapter or model regression can disable that capability without rolling back the knowledge, Calendar, or retrieval foundations.
