# App And Operations

**Status:** canonical living feature spec; native app is primary and browser UI is an off-by-default fallback
**Last verified:** 2026-07-13 against public release `0.1.2` code snapshot `dab0883`
**Owns:** daemon, scheduler, connector operations, native/browser UI, settings, provisioning, packaging, migration, and operational retention

## Product Shape

`PKM Brain.app` is a menu-bar-resident macOS application supervising one Python daemon for one Brain home.

- Closing the main window keeps the app, daemon, and jobs running.
- Quitting the app stops the supervised daemon and jobs.
- The CLI remains the developer/power surface.
- The `brain-mcp` shim provides normal agent access and read-only fallback when the app is unavailable.
- The browser UI remains available behind `--serve-web` as a portability/debug fallback.

Swift is a client. It never opens SQLite, LanceDB, raw sources, or policy files directly.

## Process Boundary

```text
PKM Brain.app
  SwiftUI views
    -> PKMBrainKit APIClient
      -> loopback bearer-auth JSON API
        -> Python service/action/question/memory primitives

  DaemonSupervisor
    -> app-managed Python runtime
      -> brain daemon
        -> serial scheduler + optional browser server
```

The daemon writes a per-home handshake containing port, PID, token path, runtime version, and home identity. It holds a single-instance lock and exits when its supervising parent disappears.

## Daemon And Scheduler

The daemon:

- initializes/validates schema and home;
- generates a per-boot API token;
- serves health, scheduler, UI, settings, and operations endpoints;
- executes mutation-capable jobs through a serial executor;
- exposes skipped/no-op reasons, not only status labels;
- keeps jobs role-gated.

Current job classes include capture tick, nightly maintenance, secondary tick, and one `sync:<peer>` job per configured child on a primary. The registry derives cadence and due state from config. Pausing does not erase due state; run-now behavior is explicit.

The app daemon replaces normal LaunchAgent automation. Legacy LaunchAgent commands remain only for rollback/development and must identify themselves as deprecated when an app-managed daemon exists.

## Connector Operations

The connector registry wraps built-in capture adapters behind one manifest/config/health interface.

Each connector exposes:

- stable ID and display name;
- capability/source type;
- enabled state and cadence;
- last run/status/error;
- consecutive failures;
- config schema suitable for a native form.

One connector failure does not abort other connectors or the scheduler tick. Connectors only write inbox artifacts and capture state.

The Python registry exists. Full native connector configuration cards remain incomplete.

## Native Information Architecture

The main `NavigationSplitView` has seven destinations:

1. Today
2. Queue
3. Wiki
4. Entities
5. Ask
6. Ops
7. Settings

The Navigate menu binds Command-1 through Command-7. The richer Command-K palette and keyboard help overlay are planned, not implemented.

The UI is work-focused: compact lists, evidence-first detail, stable split-pane dimensions, semantic status colors, no decorative marketing surfaces, and no knowledge mutation outside existing API primitives.

## Shared UI State

Global status, sidebar badges, menu bar, Today, and destination pages must not maintain contradictory projections of the same server state.

- Queue count uses the deduplicated, retrievable active Queue definition.
- Opening or deciding Queue items reconciles global count immediately.
- A stale digest may be labeled with its generated time but may not remain the sidebar authority after a fresher Queue response.
- daemon restart replaces stale API state and reloads the active destination without requiring navigation away and back.
- timestamps shown to humans use a readable local/relative presentation while raw ISO values remain available for inspection.

## Today

Today shows:

- daemon/scheduler health;
- latest nightly and eval/audit status;
- index/embedding state;
- sync state when configured;
- review counts;
- recent fact/page deltas.

Pulse items link into the owning destination. Errors remain specific and actionable. The current implementation is functional, but its Queue counts inherit the global-state reconciliation requirement above.

## Queue

Queue is the primary human workflow. Its data/action contract lives in [Curation And Review](curation-and-review.md).

Native requirements:

- bounded 50-item pages with Load More;
- server-side retrieval/priority/newest sort before pagination;
- working group filters;
- split list/detail;
- optimistic decision with rollback;
- undo countdown;
- complete policy/conflict/topology/inbox/memory/audit cards;
- labeled fact source dates with direct/chunk provenance and an explicit unavailable state;
- selection and homogeneous batch mode;
- visible loaded/total/resolved/skipped progress;
- an immediate opaque loading state for group/state/sort transitions, with stale response rejection and disabled row/keyboard actions until the selected response arrives;
- keyboard navigation and visible context-specific keys;
- accessible confidence and retrieval badges.

Current implementation includes the split pane, paging, sorting, filters, decision/undo paths, page-split previews, policy cards, popularity, confidence bands, labeled fact source dates, extraction-anomaly alerts, sampled-audit findings, and Review/Needs Repair/Deferred modes. Anomaly cards expose document/block-rate context and alert-only decisions. Native and browser audit cards expose the auditor rationale plus an Applied Change panel: topology direction, current page/contract status, affected fact/page/contract counts, and representative facts. Current actions offer guarded Revert/Keep effects; a drifted but still-active audited fact offers targeted Reject Applied Fact, while obsolete drifted topology findings are absent. The default Review mode contains only approvable admitted work; blocked cards remain inspectable with disabled controls under Needs Repair, and excess future work remains inspectable under Deferred. The Queue summary is PID/home-bound across digest, Queue load, decision, undo, and daemon replacement.

Resolved in the current implementation through 2026-07-13:

- sidebar/menu/Today authority now uses the freshest server Queue summary instead of the independent digest integer;
- candidate-less or otherwise incomplete cards remain visible with a reason but cannot be submitted through either client or a direct mutation POST;
- direct document and chunk-backed provenance now supplies a labeled source date to native and browser fact cards;
- entity merges show source-to-destination direction and active entity status.
- symmetric historical fact groups render as independently selectable Historical Facts; any nonempty subset may remain active, so the UI does not force one winner or an all-facts choice.
- Queue group/state/sort transitions now carry a request identity, cover stale list/detail content with a loading state, reject out-of-order responses, and block stale mouse/keyboard decisions while loading.
- sampled-audit rendering and admission now share the applied-state guard: topology findings include complete current context, stale topology findings disappear, and drifted fact findings use a reversible targeted correction only while the exact audited fact remains active.
- applied entity-merge audit cards validate the expected post-state (active destination, merged sources) rather than incorrectly requiring both entities to remain active.
- unapplied entity-merge proposals disappear from Queue rows and counts once any fully hydrated target entity becomes inactive, instead of reappearing as a disabled Needs Repair card after later topology changes.
- native Inbox route-candidate number badges are registered as window shortcuts instead of depending on hidden-view focus. Candidate, Reject, and Skip shortcuts suspend while the custom page-path field is focused; Return submits a nonempty custom path. Native and browser custom-route fields autocomplete substring matches from the active routable Wiki-page pool.

Remaining acceptance gaps:

- a signed-app, observed keyboard-only 20-item acceptance pass is still missing;
- relation-aware batch actions are not implemented.

The current signed screenshot pass covers the live actionable/repair mode control and source-dated Queue cards in dark appearance. Historical subset-selection controls bind each visible number directly to its fact toggle and Return directly to Keep Selected, independent of current button focus. Earlier temp-home coverage includes blocked cards. Automated light/dark, minimum-width, accessibility, and focus coverage remains owned by TEST-1.

## Wiki

Target:

- namespace/type/status tree and search;
- real Markdown rendering;
- citation markers with provenance popovers;
- source opening plus Confirm/Flag actions;
- page contract rail;
- snapshot diffs;
- all facts discoverable without a silent cap.

Current native implementation has searchable page list/detail, `swift-markdown` 0.8 rendering, explicit 20-at-a-time fact disclosure with an honest total, verbatim quote/source detail, Finder source reveal, Confirm/Flag actions, a page-contract disclosure, and recent before/after snapshot previews. Stable in-body citation markers, full snapshot diffs, Entity deep links from facts, and focused renderer interaction tests remain open.

The browser fallback has a richer renderer, but browser capability does not satisfy native acceptance.

## Entities

Target and current strengths:

- searchable active/inactive index;
- type filter;
- sort by distinct retrievals, fact count, recency, or name;
- detail with aliases, co-mentions, facts grouped by page;
- fact sort by retrieval, confidence, or observation time;
- accessible popularity/confidence cues.

Merge candidates now render structured direction, evidence signal, risk, score, and affected-fact count. `Propose Merge` calls `/api/entities/merge`; Swift never writes entity tables, and the resulting action follows the normal policy/Queue path. Alias removal and richer status management remain absent because no equivalent reversible service primitive exists yet.

## Ask

Ask is a retrieval console, not chat:

- task input;
- mode selector;
- debug toggle;
- explicit verdict and confidence;
- separate facts/pages/chunks/active memories/candidate memories;
- selection reasons;
- bounded local history.

Current native Ask implements those primary sections, exposes retrieval debug/suppression detail behind a disclosure, and links returned pages/facts/entities and chunk source paths to their owning native view or Finder. A fact can only link to an Entity when retrieval returns `entity_id`; source navigation similarly requires `raw_context`.

## Ops

Target Ops consolidates:

- scheduler run-now/pause/resume and next-due state;
- automation run history and summaries;
- connector health/config;
- action ledger and guarded revert;
- policy versions and audit findings;
- page contracts;
- index/embedding doctor and maintenance;
- sync peer matrix;
- logs;
- runtime versions, diagnostics, and prune dry-run.

Current native Ops has segmented Scheduler, Runs, Connectors, and Storage views. It supports job run-now, pause/resume, automation/ingestion history, connector enable/disable/run, active/deferred review counts, and managed/runtime/backup storage inventory through `GET /api/ops/storage`. Action-ledger revert, policy/audit/contracts, index doctor, sync, and logs still require native sections.

In-app explanatory copy should not substitute for controls or status. The final Ops view should expose the action directly and keep raw JSON/log detail behind disclosure.

## Settings

Implemented:

- Entity and Fact Autonomy segmented control;
- last-saved date/time, confidence floor, future-only scope, and hard-boundary summary;
- one Topology Bias slider that inversely maps merge versus page-split candidate admission for future gardener runs only;
- one Topology Review Threshold stepper that controls the future-job size boundary for mandatory human topology review without weakening cross-type, contradiction, confidence, critic, or eval gates;
- validated Brain-home draft, directory chooser, confirmation, daemon health check, persistence, and rollback;
- browser fallback toggle;
- notifications;
- login item;
- daemon status/restart.

Internal policy versions remain available to backend diagnostics but are not presented as a user setting. After a successful Settings write, the view says `Changes saved at <time>` and updates its `Last saved <date/time>` header.

The autonomy contract is defined in [Curation And Review](curation-and-review.md).

Open:

- connector forms;
- embedding model/index manager;
- agent/MCP registration status and repair;
- sync role/peer roster;
- diagnostics/export;
- profile selection.

Editing the home text field must not imply that a running daemon has changed homes before validation and restart complete.

## Browser Fallback

`brain ui` and daemon `--serve-web` serve static assets from `src/pkm_brain/ui_static/` over the same loopback token-auth API.

The browser remains:

- off by default in normal app operation;
- free of a separate backend or approval store;
- maintained enough to support platform portability and diagnostics;
- subject to the same queue/count/evidence contracts as native.

Static API and design-token comments point to this feature spec.

## API Surface

Key UI endpoints:

| Endpoint | Purpose |
|---|---|
| `GET /api/digest` | Today/global pulse and deltas |
| `GET /api/queue?kind=&sort=&limit=&cursor=` | deduplicated complete review cards |
| `POST /api/queue/<id>/decision` | dispatch to owning primitive |
| `POST /api/queue/undo` | guarded revert/reopen |
| `GET /api/wiki/pages` and page detail | Wiki browsing/provenance |
| `GET /api/entities` and entity detail | entity/fact browsing and popularity |
| `POST /api/entities/merge` | policy-gated merge proposal |
| `POST /api/retrieve` | retrieval packet |
| `GET|PUT /api/settings/curation` | future-action autonomy policy |
| health/scheduler/connectors/ops/sync endpoints | operations |

All writes require auth and stable human-readable errors plus machine codes where appropriate.

## Runtime Provisioning And Packaging

The app provisions a pinned Python runtime under:

```text
~/Library/Application Support/PKM Brain/runtime/
```

Provisioning checks bundle, local cache, then network. It smoke-tests `brain --version` before activation and replaces a supervised daemon when either semantic version or immutable runtime ID differs. The app reuses the normal Hugging Face cache unless an app-managed model directory exists, avoiding silent zero-vector fallback after packaging.

`scripts/build-app.sh` builds the exact project-version wheel, replaces stale runtime resources, resolves pinned Swift packages through absolute app-local cache paths, generates/builds the Xcode project, ad-hoc signs nested binaries and the app, and writes `dist/PKM Brain.app`. `scripts/install-app.sh` stages and verifies the bundle in `/Applications`, keeps one previous app rollback, refreshes the login item, and optionally activates the installed build.

Runtime versions are immutable deployment artifacts. Successful activation starts process-aware retention: current, one rollback, and every runtime referenced by a live process remain; inactive older versions are removed. Process inventory failure is fail-closed, and process output is drained before waiting for exit so large inventories cannot deadlock the retention task.

The verified `0.1.1` activation reduced app runtimes from 36 directories and 34.71 GB to 6 directories and 5.83 GB, reclaiming about 28.9 GB. It retained the new current runtime, one rollback, and all four older runtime IDs referenced by 12 live MCP/resource-tracker processes. User-created Brain backups, migration backups, and sibling runtime backups were not deleted.

## Migration And Rollback

The app migration:

- adopts the existing `~/brain` without moving private data;
- backs up and retires role-specific LaunchAgents;
- writes a rollback script;
- installs `brain` and `brain-mcp` shims;
- rewrites agent MCP registration only after confirmation;
- verifies a live MCP round trip;
- supports primary and secondary role sets.

Rollback restores launchd operation without rewriting Brain data.

## Security

- loopback only unless an explicit debug override is used;
- per-boot token, owner-only file;
- no Swift database access;
- notifications exclude content;
- logs retain existing redaction;
- no analytics;
- daemon and app termination do not leave orphan supervised processes.

## Frontend Test Contract

Swift unit tests verify model/fixture decoding, daemon supervision, runtime identity replacement, and process-aware retention. XCUITest launches an isolated temporary Brain/app-support home under a dedicated bundle ID, explicitly opens the main window for fresh menu-bar state, renders all seven destinations, captures screenshots, and exercises the Queue number/Return path. CI runs both suites on macOS in addition to Python tests on Ubuntu.

Concurrent health polling, destination loads, and notification delivery must not share mutable response decoders or capture main-actor state in arbitrary-queue completion handlers. Failed CI UI runs retain the result bundle and macOS diagnostic reports.

Required coverage for completion:

- seeded screenshots at minimum and normal window widths in light/dark appearance;
- count reconciliation after load, decision, undo, and daemon restart;
- keyboard-only mixed Queue pass;
- card completeness/disabled invalid actions;
- Markdown/provenance interaction;
- Entity merge proposal;
- Ask negative result and debug disclosure;
- Ops commands with confirmation/error states;
- Settings persistence and future-only autonomy semantics;
- accessibility labels for color-coded confidence and icon-only controls.

## Acceptance

- app start/quit leaves exactly one supervised daemon process while running and none after quit;
- Today, menu bar, sidebar, and Queue agree on active review count;
- no approvable Queue card lacks required evidence;
- all seven destinations have implemented controls or are honestly marked incomplete in the UI/spec;
- a native Wiki citation reaches its source and supports Confirm/Flag;
- Ops reaches scheduler, logs, policy/audit, index, sync, connectors, and maintenance;
- runtime activation prunes old versions while retaining current and rollback;
- browser fallback remains API-compatible and off by default.

Verification:

```bash
swift test --package-path app
uv run pytest tests/test_daemon.py tests/test_ui_auth.py tests/test_ui_endpoints.py -q
scripts/m2-clean-machine-acceptance.sh
scripts/m3-migration-acceptance.sh
scripts/build-app.sh
```
