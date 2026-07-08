# PKM Brain.app — macOS Application Spec

**Status:** implementation spec for Codex — design decisions in §0 are fixed (locked with Peter, 2026-07-07); implementation choices within them are Codex's
**Last verified:** 2026-07-08 against implementation commits `d82c264` (M0), `ad62b81` (M1), `8c6f518` (M2 base), `281e5e2` (M0-M2 audit hardening), and `03a2adf` (M3 migration + MCP proxy); UI v2 landed in `b337049`
**Author:** Claude, from Peter's decisions
**Design authority carried over:** `docs/brain-ui-v2-spec.md` §2–3 (information architecture, view content contracts, queue keyboard model) remains the UX source of truth. This spec re-targets those decisions from web to native SwiftUI and adds the app shell, scheduler, connectors, packaging, and migration. `docs/project-audit-2026-07-07.md` items 21–24 are unaffected.
**Companion:** `docs/brain-topology-and-role-mobility-spec.md` — multi-child topology, primary promotion/demotion/handover, and multi-profile isolation (work vs personal brains on one Mac). This spec carries the app-side hooks (§3.2 per-peer jobs, §10.1, §11); the topology spec carries the protocols and prerequisites.

---

## 0. Decisions locked (from Peter, 2026-07-07)

1. **UI: full native SwiftUI.** All six destinations (Today, Queue, Wiki, Entities, Ask, Ops) rebuilt as native views over the existing JSON API. The UI v2 web implementation is **kept, not deleted** (decision updated 2026-07-08): it becomes an off-by-default fallback behind `--serve-web`, maintained as the platform-portability hedge — it is the working platform-agnostic frontend if the daemon ever runs on a non-Mac box, and it costs little because both UIs consume the identical endpoints covered by the same test suite.
2. **Runtime: app-managed.** Small .app (~20MB + bundled model). The Python environment is provisioned into `~/Library/Application Support/PKM Brain/runtime/` from a pinned lock using a bundled `uv`. The provisioner reads seeds in order **bundle → local cache → network**, so a fully self-contained `--bundle-runtime` build variant can be added later without architectural change.
3. **Embeddings: bundle the default, allow custom, never force a blind rebuild.** The default model (`BAAI/bge-small-en-v1.5`, ~130MB of plain weight files) ships inside the .app and is seeded into the app-managed models directory. Users can configure any local sentence-transformers model. **Each embedding identity gets its own stamped vector index**; switching models builds the new index and flips an active pointer; switching back is instant plus a missing-only catch-up. Changing the *default* bundled model is just a build-script file swap (app rebuild, no signing complexity — weights are not Mach-O).
4. **Background: menu-bar-resident app.** One process tree: the app is a login item (SMAppService), lives in the menu bar, and supervises the Python daemon which runs all jobs on the existing cadences. Closing the window keeps jobs running; **quitting the app stops jobs until next launch/login**. All pkm-brain LaunchAgents (three on the primary laptop, the `capture-secondary` set on the secondary Mac) are retired at migration.
5. **MCP: thin proxy with read-only fallback.** `brain-mcp` is a stdio→HTTP proxy to the daemon. If the daemon isn't up, it auto-launches the app in the background; if the app can't launch, it falls back to direct **read-only** DB access (retrieval works, writes fail with an actionable error).

Defaults set by this spec (change only if Peter objects):

- **Data stays at `~/brain`.** The app adopts the existing home in place; nothing moves into a container. `--home` scoping (forks, shadow homes, temp test brains) keeps working end to end.
- **Distribution: local build, ad-hoc signing, no sandbox, no notarization, no App Store.** Personal tool; sandboxing is incompatible with reading `~/.codex`, `~/.claude`, Hyprnote data, and spawning the Codex CLI.
- **The CLI survives.** `brain` remains the developer/power surface, installed as a shim to the app-managed runtime. Normal operation flows through the app + MCP proxy.
- **Minimum macOS 15**; developed and verified on Peter's current OS. Swift 6, SwiftUI + Observation, no third-party UI frameworks.
- App name **“PKM Brain”**, bundle id **`com.pkm-brain.app`**.

---

## 1. Goal & shape

Turn the prototype (repo CLI + LaunchAgents + local web UI) into a real macOS app that *is* the brain on this machine:

- the **only writer** to the knowledge store during normal operation,
- the **scheduler** for capture/nightly/sync,
- the **native review surface** (the queue is the only mandatory human work in the system — it gets the best UI),
- the **host for agent access** (MCP through a proxy),
- with **connectors** as the modular unit of ingestion, so new sources (email next) are added by writing one connector, not by touching the pipeline.

Non-goals (v1): App Store, sandboxing, iOS/iPadOS, multi-user, cloud backend, realtime collaboration, auto-update framework (rebuild-and-replace is fine), loading third-party connector code from outside the package (the registry is designed for it; dynamic loading is deferred), WYSIWYG wiki editing.

### 1.1 The Swift/Python boundary (rule, not preference)

- **Python owns knowledge and mutation:** ingest, retrieval, facts/actions/policy, curation, evals, sync, scheduling logic, connectors. Everything testable by pytest stays in Python. This code is shared verbatim with the CLI and MCP.
- **Swift owns macOS:** windows, menu bar, notifications, keyboard, Settings, login item, process supervision, runtime provisioning.
- **Swift never opens SQLite or LanceDB.** If a view needs derived data, add a JSON aggregation endpoint next to `ui_digest`/`ui_queue` in Python. No parallel data model, no parallel approval path (standing decision, same as UI v2).

---

## 2. Architecture

```
┌───────────────────────────── PKM Brain.app ─────────────────────────────┐
│ SwiftUI: MenuBarExtra · Main window (6 destinations) · Settings ·       │
│          Onboarding/Migration assistant · Notifications                 │
│    APIClient (URLSession, Codable)  ── Bearer token ──┐                 │
│    DaemonSupervisor (spawn/health/restart/backoff)    │                 │
│    RuntimeProvisioner (bundled uv, lock, seeds)       │                 │
└───────────────────────────────────────────────────────┼─────────────────┘
                                                        ▼ 127.0.0.1:<ephemeral>
┌──────────────────────── brain daemon (Python) ──────────────────────────┐
│ HTTP JSON API (existing ui_server endpoints + §3.3 additions)           │
│ Internal scheduler (capture tick · nightly due-check · sync · retention)│
│ Connector registry (§5)  ·  BrainService  ·  single-writer job queue    │
└───────────────┬───────────────────────────────┬─────────────────────────┘
                ▼                               ▼
        ~/brain (SQLite WAL, LanceDB      ~/Library/Application Support/
        stamped indexes, raw/, wiki/,     PKM Brain/ (runtime/, models/,
        inbox/, logs/, config/)           bin/ shims, migration backups)

brain-mcp (stdio proxy) ──HTTP──▶ daemon   (fallback: read-only BrainService)
brain CLI ────────────────────────▶ daemon-first for writes; direct DB as dev path
```

Process model: the daemon is a **child process of the app**. App quit (⌘Q) terminates the daemon (graceful `POST /api/shutdown`, then SIGTERM after 5s). Window close (⌘W) hides the window; the menu bar item and daemon keep running. Login item relaunches the app (menu-bar only, no window) at login.

---

## 3. The Python daemon (`brain daemon`) — new command, mostly existing code

`ui_server.py` already is the JSON API. The daemon wraps it with lifecycle, identity, and scheduling. Implement as `brain daemon [--home PATH] [--serve-web] [--port N]`.

### 3.1 Boot, handshake, identity

- Bind `127.0.0.1` on an **ephemeral port** by default (`--port 0` semantics); fixed port only via flag.
- Generate a **per-boot token** (replaces the persistent `ensure_ui_token` file for daemon use; `ensure_ui_token` stays for the standalone `brain ui` command, which remains the headless/remote-debug way to serve the web UI).
- Write a **handshake file** `~/brain/config/local/daemon.json`, mode `0600`, atomically:
  ```json
  {"pid": 123, "port": 54321, "token": "…", "version": "0.2.0",
   "home": "/Users/Peter/brain", "started_at": "…"}
  ```
  Per-home handshake means forks/shadow brains can each run their own daemon. Remove the file on clean shutdown; treat a stale file (dead pid) as absent.
- **Single-instance lock** per home (flock on the handshake file or a sibling lockfile). Second daemon for the same home exits with a clear error.
- Structured logs to `~/brain/logs/daemon.log` (reuse existing log conventions; no raw document contents — same redaction posture as today).

### 3.2 Internal scheduler (replaces the LaunchAgents on every node role)

A single scheduler thread + **serial job executor** (one job at a time — this is what makes the single-writer promise real; it also retires the `database is locked` class from overlapping launchd jobs).

Job registry (ids are stable API; **composed per node role at daemon boot** — role from `config/sync.yaml`, `single` when absent — so a secondary daemon has no `sync` job to accidentally enable):

| job id | role | cadence | body (reuse, don't rewrite) |
|---|---|---|---|
| `capture_tick` | single, primary | 600s | per-connector capture (enabled connectors only) + `ingest` — parity with `automation run-agent-log-ingest` |
| `secondary_tick` | secondary | 600s | `run_secondary_tick(...)` as-is: per-connector capture **with outbox export** + local ingest + index status. This *is* the capture tick on a secondary — `capture_tick` is absent there. Its existing flock guard is redundant under the serial executor but harmless; keep it |
| `nightly` | all | hourly due-check, `due_after_hours=20` | `run_nightly_maintenance(...)` unchanged, **including** its stage list, telemetry-retention stages, and the existing role gate (on a secondary, mutation-capable CoS stages record `status: skipped` with `cos_role`) |
| `sync:<peer-node-id>` | primary | per peer from `config/sync.yaml` (default 1800s) | one registry entry **per configured peer**: `brain sync run <peer> --if-reachable` — the Primary initiates both pull and push; unreachable = success-with-flag, not failure. This closes the sync spec's deferred "multi-Secondary per-peer scheduler labels" item: launchd needed a plist per peer, the registry just iterates the roster. Transfers stay on the serial executor in v1 (ingest-after-pull must serialize anyway) |
| `embedding_flip` | all | on demand | §6.4 flow; runs as a normal queued job with an `automation_runs` record |

Semantics preserved exactly: laptop-friendly due-check (`--if-due` logic — the machine sleeping through the night is caught up on the next tick), `automation_runs` recording, warning-vs-error blast-radius rules from audit item 3. Wake handling: subscribe the scheduler tick to a 30s timer; after system sleep the next tick evaluates due-ness (same behavior launchd's `StartInterval` gave us, minus launchd).

Controls: pause (until timestamp, persisted to `config/local/scheduler.yaml` so a crash doesn't unpause), resume, run-now (enqueue), per-job enable/disable.

### 3.3 API additions (everything else already exists — see inventory below)

- `GET /api/health` → `{ok, version, home, pid, started_at, schema_version}` — **must not** trigger embedding-model load (model loads lazily on first semantic query; health stays <50ms).
- `GET /api/scheduler` → jobs with `{id, enabled, cadence_s, last_run_at, last_status, next_due_at, running}`, plus `paused_until`.
- `POST /api/scheduler/run {job_id}` · `POST /api/scheduler/pause {seconds}` · `POST /api/scheduler/resume` · `POST /api/scheduler/jobs/{id}/enable|disable`.
- `GET /api/connectors` · `GET /api/connectors/{id}` · `POST /api/connectors/{id}/enable|disable|run` · `PUT /api/connectors/{id}/settings` (§5).
- `GET /api/embeddings` · `POST /api/embeddings/models/download` · `POST /api/embeddings/flip` · `POST /api/embeddings/stamps/{stamp}/delete` (§6).
- `POST /api/shutdown` (token-authed; supervisor use).
- Existing surface the native app consumes as-is — GET: `status, setup, sync/status, sync/conflicts, jobs/status, logs, memory, memory/{id}, digest, queue, search, entities, entities/{id}, wiki/pages, wiki/page, wiki/facts, wiki/facts/page, cos/policy, cos/actions, cos/review, cos/contracts, cos/audit, ops/runs`; POST: `retrieve, queue/undo, queue/{id}/decision, entities/merge, actions/{id}/revert, wiki/facts/{id}/confirm|flag, wiki/page, wiki/questions/*, cos/questions/*, cos/contracts/*, cos/audit/*, memory/{id}/approve|reject|archive`.
- **No SSE/WebSocket in v1.** The app polls: `/api/health` every 5s (supervisor), `/api/scheduler` + `/api/digest` every 30s or on window focus. Loopback polling is free; keep the stdlib server simple.
- Web UI: `--serve-web` serves the static `ui_static/` app; default off once M2 ships, but the web UI is **maintained, not retired** — it is the platform-portability fallback (§0 decision 1). Endpoint changes must keep it working (it shares the endpoint test suite; six-view manual pass required at M6 and on any endpoint-shape change).

### 3.4 Tests (pytest, temp homes)

Boot/handshake/lock lifecycle; token auth on new endpoints; scheduler due-math (freeze time); serial executor (two run-now jobs never overlap); pause persistence; nightly parity (daemon-run nightly produces the same `automation_runs` stage summary shape as `brain automation nightly`); **role-aware registry composition** (temp home with a secondary `sync.yaml` → `secondary_tick` present, `capture_tick`/`sync` absent; daemon-run nightly summary shows mutation-capable CoS stages `skipped` with the secondary `cos_role`); health-endpoint-doesn't-load-model regression (assert no sentence-transformers import side effect).

---

## 4. The native app (Swift)

### 4.1 Project

- Lives in-repo at `app/`. Generated Xcode project via **XcodeGen** (`app/project.yml` checked in; `.xcodeproj` gitignored) — declarative, merge-friendly, agent-friendly. Prereq: `brew install xcodegen`.
- Targets: `PKM Brain` (app), `PKMBrainKit` (framework: APIClient, models, supervisor — unit-testable), `PKMBrainKitTests`.
- Layout:
  ```
  app/project.yml
  app/Sources/App/            # @main, scenes, AppState
  app/Sources/Kit/            # APIClient, Codable models, DaemonSupervisor,
                              # RuntimeProvisioner, MigrationAssistant, Keychain? (no: token comes from handshake file)
  app/Sources/Views/{Today,Queue,Wiki,Entities,Ask,Ops,Settings,Onboarding}/
  app/Resources/models/bge-small-en-v1.5/   # bundled default weights (build script populates)
  app/Resources/bin/uv                       # bundled uv (arm64)
  app/Resources/runtime/{requirements.lock, python-version, pkm_brain-<v>.whl}
  app/Tests/
  scripts/build-app.sh        # wheel build, lock export, xcodegen, xcodebuild, ad-hoc sign
  ```
- `make app` (build), `make app-run`, `make app-test` targets in the repo Makefile/justfile alongside existing commands.

### 4.2 Core services (PKMBrainKit)

**RuntimeProvisioner** (§7): ensures `runtime/current` exists and matches the app's pinned lock hash before the daemon can boot; publishes progress (download %, phase) for onboarding UI.

**DaemonSupervisor:**
- Spawn `runtime/current/bin/brain daemon --home <home>` via `Process`, env: `SENTENCE_TRANSFORMERS_HOME=<AppSupport>/models`, inherit nothing else surprising.
- Read handshake file (poll ≤10s for appearance), then `GET /api/health`. Version handshake: refuse a daemon whose `version` ≠ the app's bundled wheel version (forces re-provision, prevents skew).
- Health poll every 5s; on crash/unresponsive: restart with backoff 1,2,4,…,60s; after 3 consecutive failures post a user notification and set menu-bar state to error.
- Graceful shutdown on app quit: `POST /api/shutdown`, SIGTERM after 5s, SIGKILL after 10s. Never leave an orphan (use process group).
- If a live daemon for the home already exists (valid handshake, healthy, version matches): **adopt it** instead of spawning (covers app relaunch while daemon survives a crash of the UI process — rare since it's a child, but adoption also enables dev workflows).

**APIClient:** URLSession + Codable structs mirroring the JSON API 1:1 (field names match Python; no client-side renaming). All calls carry the handshake token. Fixture-based decoding tests: capture real responses from a seeded temp brain into `app/Tests/Fixtures/*.json` (script provided) so Python payload drift fails Swift tests.

### 4.3 Scenes

**MenuBarExtra** (always present): status glyph — ok / job running / attention (nightly failed, connector failing, daemon down) / paused. Menu: last capture + last nightly ("4h ago ✓"), queue count ("Review 42 items" → opens Queue), Run capture now, Pause jobs (1h/until resumed), Open PKM Brain, Quit (with "quitting stops background capture" subtitle).

**Main window:** `NavigationSplitView`. Sidebar: Today (⌘1), Queue (⌘2, badge = queue count), Wiki (⌘3), Entities (⌘4), Ask (⌘5), Ops (⌘6). ⌘K command palette (jump to page/entity/action; MenuBarExtra actions). `?` overlay showing the keyboard map. Content contracts per view are **exactly** `brain-ui-v2-spec.md` §3.1–3.6 — implement those data requirements against the same endpoints; this spec only adds native mechanics:

- **Today:** digest cards from `/api/digest`; pulse row is the same data the menu bar uses; every delta links into Queue/Wiki.
- **Queue (the centerpiece; most engineering care goes here):** split-pane list/detail from `/api/queue`; full keyboard model from v2 §3.2 (j/k or arrows navigate, a/r/s decide, u undo within the window, x multi-select, ⇧A batch, esc clears) implemented as SwiftUI `.keyboardShortcut` + a focused key-handling view so it works without mouse; optimistic decision dispatch to `POST /api/queue/{id}/decision` with rollback on error; undo via `queue/undo` with a visible countdown; conflict cards render both facts side-by-side with evidence quotes; progress header (n of m today).
- **Wiki:** render Markdown natively — SPM `swift-markdown` (cmark) → `AttributedString`; **provenance popovers** are `NSPopover`-style popovers on citation markers showing the verbatim evidence quote + source link (opens raw file in Finder/default editor) + Confirm/Flag buttons wired to `wiki/facts/{id}/confirm|flag`; contract rail; snapshot diffs (before/after, monospaced). Renderer gets escape/fixture tests (same fixtures as web renderer where applicable).
- **Entities:** index with type/mention filters from `/api/entities`; detail with linked facts/pages; merge proposal UI → `entities/merge` (lands as a normal policy-gated action — never a direct write).
- **Ask:** `POST /api/retrieve`; render the packet honestly: verdict banner (`found/partial/no_strong_match` — negative results must *look* negative), memories/facts/pages/chunks sections, suppressed items inspectable, selection reasons visible on expand, query history (local, in app state).
- **Ops:** scheduler table (from `/api/scheduler`) with run-now/pause; connectors (§5.5); sync status/conflicts — on a primary a **per-peer matrix** (reachable, last pull, last push, mirror freshness, outbox depth at last contact), on a secondary mirror freshness + outbox depth (§10.1); logs viewer (tail `/api/logs`); policy/audit/contract views (existing endpoints); runs (`/api/ops/runs`); runtime panel (§7: version, re-provision, open logs); maintenance (prune dry-run report → confirm, from audit item 17's `brain maintenance prune`).

**Settings (⌘,):** General (login item toggle via SMAppService, notification prefs, home path display + "switch home" for forks), Connectors (§5.5), Embeddings (§6.5), Agents (MCP registration status for Codex/Claude, the exact registration commands with copy buttons, proxy health check button), Sync (role; **peer roster** — add/remove/pause a child, per-child cadence and status; add-child wizard wrapping the existing `add-peer` → `test-connection` → `acceptance` chain), Advanced (serve-web toggle, daemon port/pid, re-provision runtime, export diagnostics bundle).

**Onboarding/Migration:** §10. Shown when no state exists or legacy LaunchAgents are detected.

**Notifications** (UserNotifications, each with a "notify me" pref): nightly failed (with reason); connector failing ≥3 consecutive runs; daemon crash-looping; queue backlog crossed threshold (default 100, weekly at most); migration/flip completed. Clicking deep-links to the relevant view.

### 4.4 App behavior details

- Activation: regular app with Dock icon while a window is open; closing the last window keeps menu-bar presence (standard `MenuBarExtra` + window lifecycle; do not fight AppKit — `LSUIElement` stays false, rely on login-item + menu bar residency).
- Performance: cold launch → usable Today < 3s on a provisioned machine (daemon boot ~1–2s; embedding model loads lazily in the daemon on first semantic query, never on boot).
- All times shown local with relative form ("4h ago"); all counts come from the API (Swift computes nothing about knowledge).

---

## 5. Connector architecture (the modular ingestion unit)

Formalize what `capture.py` already half-has (`AgentLogAdapter` Protocol, `capture.py:89`, with Codex/Claude/OpenCode/Hyprnote adapters) into a first-class registry. **Python-side plugin seam; declarative settings so the native UI renders any connector generically.**

### 5.1 Protocol & manifest

```python
class Connector(Protocol):
    manifest: ConnectorManifest
    def preflight(self, ctx: ConnectorContext) -> PreflightReport: ...   # paths exist? app installed? creds?
    def discover(self, ctx: ConnectorContext) -> list[SourceCandidate]: ... # cheap; no writes
    def capture(self, ctx: ConnectorContext, candidates) -> CaptureBatch: ... # writes normalized files to inbox/<connector_id>/
```

```python
@dataclass(frozen=True)
class ConnectorManifest:
    id: str                      # "codex", "claude", "opencode", "hyprnote", "files", "email-imap" (future)
    display_name: str
    description: str
    source_type: str             # documents' source_type it produces
    default_enabled: bool        # hyprnote/email: False (opt-in stays opt-in)
    default_cadence_s: int       # capture_tick multiples
    settings_schema: list[SettingField]   # declarative: key, label, kind(bool|string|path|secret|choice), default, help
    permissions_note: str        # human-readable: what it reads on disk
```

Rules (unchanged invariants, restated for connector authors):
- Connectors **only write normalized Markdown/text into `inbox/<connector_id>/`** + update `capture_sources` state. They never touch `raw/`, SQLite knowledge tables, or indexes — ingest remains the single pipeline entry.
- Idempotent by content hash; latest-snapshot retention semantics for session-log types are pipeline behavior, not connector behavior.
- Redaction of secret-shaped values happens in the shared capture sanitizer (as today), not per-connector.
- A broken connector fails *its own* run record — never the whole capture tick (blast-radius rule).

### 5.2 Registry & config

- Built-in registry: dict in `connectors/__init__.py` mapping id → factory. v1 ships `codex`, `claude`, `opencode`, `hyprnote` (wrapping the four existing adapters — refactor, don't rewrite; capture output paths and `capture_sources` keys must remain byte-compatible so state carries over) plus **`files`** (the inbox-drop folder made explicit: watches nothing, just documents the contract that anything placed in `inbox/documents/` is ingested).
- The **email connector** (per `docs/email-ingestion-spec.md` Phases 1–3) is the first proof that the seam works: it must land as a connector without pipeline changes.
- Per-connector state in `config/local/connectors.yaml`: `{id: {enabled, cadence_s, settings{…}}}`. Secrets (future email creds) go in macOS Keychain via the app; the daemon receives them per-run over the authed API, never persisted to yaml (design the `SettingField.kind == "secret"` path now, use it in the email phase).
- Migration shim: existing `capture agents --agent X` CLI keeps working, delegating to the registry.

### 5.3 Scheduling & health

`capture_tick` iterates enabled connectors (each with its own cadence gate), records per-connector outcomes into the run summary (`{connector_id, discovered, captured, skipped, errors}`), and maintains a rolling health state (`ok | warning | failing(n)`) the API exposes.

### 5.4 API

`GET /api/connectors` → manifests + state + health + last run. `POST /api/connectors/{id}/run` (enqueue), `/enable`, `/disable`, `PUT /api/connectors/{id}/settings` (validated against the schema).

### 5.5 Native UI

Settings → Connectors renders a card per manifest: icon, name, description, enabled toggle, cadence, health badge, last capture stats, "Run now", and a form generated from `settings_schema` (this is why the schema is declarative — adding a connector requires **zero Swift changes**). Preflight failures render as inline guidance ("Hyprnote not found at …").

### 5.6 Definition of "adding a connector" (acceptance for the architecture)

A new source = one Python module (manifest + three methods) + tests. No changes to scheduler, ingest, UI, or API code. M6 acceptance includes a demo `files-watch` variant or fixture connector proving this.

---

## 6. Embeddings & model management

### 6.1 Bundled default

- Build script copies `BAAI/bge-small-en-v1.5` weights into `app/Resources/models/`. First launch seeds them into `~/Library/Application Support/PKM Brain/models/` (idempotent, hash-checked).
- Daemon runs with `SENTENCE_TRANSFORMERS_HOME` pointed at that models dir; the existing HF cache remains a read fallback so Peter's current machine needs no re-download.
- Updating the bundled default later = replace the files in Resources + rebuild the app (plain data files; no signing complexity).

### 6.2 Custom models

Settings → Embeddings offers a small curated list + free-form sentence-transformers model id. `POST /api/embeddings/models/download` fetches into the models dir (progress surfaced); dimension and stamp recorded on completion. `hash` provider remains available (offline/deterministic fallback, used by tests).

### 6.3 Per-stamp indexes (the "alternative index" design)

- Layout: `~/brain/indexes/lancedb/<stamp>/` where `<stamp>` = the existing provenance stamp (`sentence-transformer:BAAI/bge-small-en-v1.5`, slugified). Active stamp recorded in `config/local/config.yaml` (`embedding.active_stamp`).
- **Migration of the current single index:** on first daemon boot with this feature, move the existing LanceDB dir under its stamp's directory (it is already stamped from the productization work) and write the active pointer. `brain index doctor` must pass after migration. Rollback = move back.
- Ingest/reindex writes **only the active stamp's index**. Every stamp dir records a high-water mark (last synced chunk set) so inactive stamps know they're stale.
- Retention: keep the active + most recent inactive stamp by default; older stamps pruned via Ops maintenance (dry-run first, sizes shown).

### 6.4 The flip flow (guided, reversible)

`POST /api/embeddings/flip {stamp}` enqueues an `embedding_flip` job:
1. Ensure model available (download if needed).
2. Build/refresh the target stamp's index: full build if absent; **missing-only catch-up** if it exists (reuse the existing full/missing-only rebuild paths).
3. Verify row count vs SQLite chunks; run the retrieval eval suite; report side-by-side vs the current stamp's last eval.
4. Flip `active_stamp`. The old index is untouched — **rollback is a pointer flip**, instant, plus its own catch-up if time has passed.

UI copy must state the costs plainly: "Switching models builds a new index (~N chunks, est. M min). Your current index is kept for instant rollback." Eval regression (negative-control failures) blocks the auto-flip and asks.

### 6.5 Embeddings UI

Settings → Embeddings: active model card (name, dim, rows, index size, last eval scores), other stamps with Activate/Delete, custom model field, flip progress, rollback button.

---

## 7. Runtime provisioning (app-managed)

- Bundled: `uv` binary, `requirements.lock` (`uv export --frozen --no-dev --extra embeddings` at app build), `python-version`, and the `pkm_brain-<version>.whl` built from the repo at app build time. The app is **fully independent of the `~/pkm-brain` checkout** after build.
- Provision to `~/Library/Application Support/PKM Brain/runtime/<version>-<lockhash8>/`: `uv python install` (managed CPython) → venv → install wheel + locked deps. **Seed order: bundle seed dir → uv cache → network** (this is the hook that makes a future `--bundle-runtime` self-contained build a flag, not a redesign).
- Atomic activation: build in a temp dir, health-gate (`bin/brain --version` + import smoke), then symlink `runtime/current`. Previous runtime kept until the new one has served one healthy daemon boot; then eligible for cleanup.
- First-launch UX: progress screen — "Setting up the Python runtime (~500MB, one time)…" with phase + % where uv reports it; clear offline error with retry.
- App update flow: new app version carries a new wheel+lock → provisioner sees hash mismatch → provisions fresh runtime alongside → daemon restarts into it. Rollback = keep previous runtime + previous app copy.
- CLI/MCP shims installed at `~/Library/Application Support/PKM Brain/bin/{brain, brain-mcp}` (tiny scripts exec'ing `runtime/current/bin/…`), optionally symlinked into `~/.local/bin` (replacing the existing `~/.local/bin/pkm-brain` shim) during migration.

---

## 8. MCP access (`brain-mcp` proxy)

- New entry point `brain-mcp` (console script in the wheel): a stdio MCP server exposing the **identical seven tools** (`search_knowledge`, `retrieve_context`, `record_context_feedback`, `get_memories`, `propose_memory`, `write_agent_session`, `get_project_context`) with identical schemas — agents notice nothing.
- Resolution order per call session:
  1. Read `~/brain/config/local/daemon.json` → health ping → **proxy over HTTP** (each tool maps to a service call endpoint; add thin `POST /api/mcp/{tool}` passthroughs rather than re-shaping existing UI endpoints).
  2. No/st stale handshake → `open -g -a "PKM Brain"` (background, no window) → poll handshake up to 20s → proxy.
  3. Still unavailable → **read-only fallback:** instantiate `BrainService` directly with a `read_only=True` mode — retrieval tools work but **must not write** `retrieval_events`/lineage (add the flag; today `retrieve_context` writes events). Write tools (`propose_memory`, `write_agent_session`, `record_context_feedback`) return a structured error: `"PKM Brain app is not available; write declined. Launch the app and retry."` No write queueing in v1.
- Trust boundary unchanged: MCP still exposes **no approval, no wiki mutation** (hard rule).
- Migration rewrites agent registrations (§10 step 6) to the shim path. Old direct `brain mcp` command remains for dev but prints a deprecation note when a daemon handshake exists.

## 9. CLI posture

`brain` CLI ships in the runtime and stays fully functional. Post-migration guidance (help text + docs): when a daemon is running, prefer daemon-backed operation for anything that writes; direct-DB CLI use is the developer path (WAL still protects it, and the daemon's serial executor means at most two writers instead of today's N). `brain launch-agent *` commands gain a deprecation warning after M3 and are removed only when Peter says so.

---

## 10. Migration from the current setup (explicit, resumable, reversible)

The assistant runs on first launch and detects one of three states:

- **A. Migrate** — `~/brain` exists **and** any pkm-brain LaunchAgent is installed. Detection covers **both role sets**: primary/single — `com.pkm-brain.agent-log-ingest`, `com.pkm-brain.nightly-maintenance`, `com.pkm-brain.sync-primary`; secondary — `com.pkm-brain.capture-secondary`, `com.pkm-brain.nightly-maintenance`. ← Peter's Macs (laptop = the primary set; the secondary Mac = the secondary set).
- **B. Adopt** — `~/brain` exists, no LaunchAgents. Steps 4–8 only.
- **C. Fresh** — nothing. Native wrap of the existing setup wizard (`brain init --wizard --json` under the hood) **including role selection (single / primary / secondary)**; choosing a sync role hands off to the existing sync configuration flow (peer entry, SSH host-key pinning, `config/sync.yaml`), and on a secondary ends with the `brain sync acceptance` preflight. Then steps 6–8.

**State A, step by step (each step idempotent; assistant shows a checklist with per-step results; "dry run" mode prints the plan without acting):**

1. **Preflight:** runtime provisioned; `~/brain` passes doctor; schema migrations current (`run_migrations` is idempotent); disk headroom for a backup; no rebuild/regeneration currently running (check for live locks — the audit's `database is locked` memory applies here).
2. **Backup:** `brain cos backup-runtime` equivalent via daemon; record backup path in the migration log.
3. **Retire LaunchAgents:** for each **detected** label (from the full four-label list above): `launchctl bootout gui/$UID/<label>` (tolerate "not loaded"), move the plist to `~/Library/Application Support/PKM Brain/migration/plists-backup/`, and write `rollback.sh` alongside (re-`bootstrap`s the saved plists) so rollback is one script. **Show Peter exactly what is being removed before doing it.**
4. **Adopt the home:** record `~/brain` as the app's home; boot the daemon; verify `/api/health` and that **nightly due-state is preserved** — the daemon reads the same `automation_runs` watermark, so a recent nightly success must show "not due" (assert in the checklist; migration must not trigger a surprise full nightly).
5. **Login item:** register via SMAppService; confirm enabled.
6. **Agent access:** detect current MCP registrations (`codex mcp get pkm-brain`, `claude mcp get pkm-brain`), display current → proposed (`brain-mcp` shim path), rewrite on confirm (`codex mcp add …`, `claude mcp add -s user …`), then run a **live round-trip test** (spawn the shim, call `search_knowledge`, expect results). Skills/plugins need no change (they're instructions, not paths) — but update the two SKILL.md CLI-fallback snippets in a follow-up commit to mention the shim.
7. **CLI shims:** install `bin/brain`+`brain-mcp`, offer `~/.local/bin` symlinks (replacing the stale `pkm-brain` shim there).
8. **Verification checklist (all green before "Done"):** capture tick ran within 10 min and `capture_sources` advanced · search returns results in Ask · queue loads with current counts (audit baseline: ~300 needs-human items) · MCP round-trip ok from both agents · menu bar reflects scheduler state · nightly shows correct next-due time.

**Rollback (documented in-app):** quit app → run `migration/plists-backup/rollback.sh` → disable login item → (optional) restore MCP registrations from the recorded previous values (assistant saves them in the migration log). Data was never moved, so rollback touches no data.

**What does not move:** `~/brain` stays canonical (wiki stays Obsidian/Finder-browsable — that's a feature); the repo checkout stays for development; `brain-shadow`/`brain-forks` homes untouched (each can run its own daemon on demand).

### 10.1 Secondary node profile

The Secondary runs the **same app**; `role` in `config/sync.yaml` changes only the scheduler registry and a few surfaces. The sync mechanism itself is unchanged Python: Primary-initiated rsync over pinned SSH — pull `outbox/<node-id>/` → `inbox/external/<node-id>/` → ingest; push canonical `raw/`/`wiki/`/`memory/`/`config/shared/` → mirror → `sync rebuild-mirror-index`. No SQLite/LanceDB ever crosses machines; the Secondary's DB and indexes remain machine-local derived state rebuilt from mirrored files.

- **Scheduler:** `secondary_tick` (600s) + role-gated `nightly`; no sync jobs — the Primary initiates (§3.2 table).
- **Multiple children:** a Primary may have any number of children (the config's `peers` list and the per-peer transport already support it); each child is independent — its own outbox namespace, staging dir, origin identity, and `sync:<peer>` job on the Primary. Children never talk to each other (star topology only). Two children of *different* brains may share one device — see §11 and the topology spec.
- **Migration there:** same assistant; State A detects the secondary label set (`capture-secondary`, `nightly-maintenance`) and retires it with the same plist-backup + `rollback.sh` mechanics; MCP registration rewrite (step 6) applies if agents run on that machine.
- **UI:** identical six destinations over the mirror-rebuilt local index. Ops→Sync additionally shows mirror freshness (last push received, `mirror-hash` result) and outbox depth (files awaiting Primary pull).
- **Write semantics, stated honestly in-app:** MCP/UI writes on a Secondary land in that machine's local derived DB and **do not sync back** in v1 — only outbox-captured session files flow to the Primary (structured `agent_sessions`/memory back-sync remains deferred, sync spec §19). Settings→Agents and the Queue view show a role banner saying so when `role: secondary`.
- **Acceptance (extends the step-8 checklist on that node):** daemon-run nightly summary shows mutation-capable CoS stages `status: skipped` with the secondary `cos_role` · `secondary_tick` produces outbox files + `manifest.jsonl` that a Primary pull ingests (two-home simulation acceptable; the real two-machine pass follows `docs/runbooks/sync-acceptance.md`) · `sync rebuild-mirror-index` runs clean from the app-managed runtime.

---

## 11. Profiles — multiple isolated brains on one Mac

Design summary; the full analysis, requirements, and phasing live in `docs/brain-topology-and-role-mobility-spec.md` §5. Profiles are **not** scheduled into M0–M6, but nothing in those phases may preclude them — the constraints below are binding on M0–M6 design choices.

- A **profile** = one brain home + one daemon. The Python core is already home-scoped end to end (DB, indexes, raw, wiki, config, per-boot token, handshake file, `node_id`, single-instance lock) — profiles are an **app-level** concept: a registry at `~/Library/Application Support/PKM Brain/profiles.json` (`{name, home, accent}`), N supervised daemons running concurrently, a profile switcher in the window toolbar + menu bar, notifications and windows tagged/tinted per profile so "wrong brain" mistakes are visually impossible.
- **Shared read-only, isolated everything else:** the provisioned runtime and downloaded model weights are shared across profiles (they are artifacts, not data). Logs, backups, queues, telemetry, tokens stay per home.
- **The real isolation problem is capture, not storage.** Agent session sources (`~/.codex`, `~/.claude/projects`, OpenCode's DB) are device-global; naively enabling the same connectors in two profiles double-ingests every session and cross-contaminates work/personal. The connector layer therefore gains (a) a **device-source claim registry** shared across profiles — each device-global source is claimed by exactly one profile unless explicitly set to filtered — and (b) **routing rules** on agent connectors (path-prefix on session working directory → profile; the metadata is already captured: Codex `cwd`, Claude `cwd`/`project`, OpenCode `worktree`). Hyprnote has no cwd and supports exclusive claim only.
- **MCP:** one registration per profile (`pkm-brain-work`, `pkm-brain-personal` → `brain-mcp --home <home>`); the brain-memory skill tells agents to pick by task context. Residual risk stated honestly: an agent with both servers registered can move context between brains — v1 mitigates by naming + skill policy, not technical enforcement.
- **Sync colocation:** profiles are full topology citizens — a work-child and a personal-child (of different primaries) can share one device, since peer `brain_home` is honored in rsync paths and remote commands (`sync_rsync.py`, `sync_transfer.py:275`). The wizard must force explicit, distinct `node_id`s per profile: the hostname default (`paths.py:114`) collides when two homes share a machine.

---

## 12. Build, signing, packaging

- `scripts/build-app.sh`: `uv build` (wheel) → `uv export` (lock) → stage Resources (wheel, lock, uv binary, model weights) → `xcodegen` → `xcodebuild -scheme "PKM Brain" -configuration Release` → **ad-hoc codesign** (sign nested binaries first — only the app's own Mach-Os in v1 since Python lives outside the bundle; the future `--bundle-runtime` flag is where per-binary enumeration matters) → produce `dist/PKM Brain.app` + zip.
- No notarization, no Developer ID, no sandbox, no hardened runtime requirement for local use. Entitlements: none beyond defaults.
- CI: existing GitHub Actions job stays (ruff+pytest); add a macOS job for `xcodebuild build` + `make app-test` (kit tests only; no signing in CI).

## 13. Security & privacy posture (unchanged, restated)

Loopback-only daemon; per-boot token in a `0600` file; no analytics/telemetry egress; the only network calls remain: runtime provisioning (astral/PyPI), model downloads (HF), explicitly configured LLM providers, and sync over pinned SSH. Notifications must not include document contents (titles/counts only). Logs keep existing redaction rules.

---

## 14. Phased build plan (each phase lands green: `ruff` + `pytest` + `xcodebuild` + manual checklist; re-stamp touched docs per house convention)

- **M0 — Daemon (Python only).** §3 complete behind `brain daemon`; no behavior change for existing installs. *Accept:* daemon boots on a temp home with handshake/lock/auth; scheduler runs capture+nightly on a compressed test cadence with `automation_runs` parity; serial executor proven; health <50ms with no model load; `--serve-web` serves the v2 UI identically.
- **M1 — Connector registry (Python).** §5: four adapters wrapped byte-compatibly + `files`; config, health, API. *Accept:* `capture_sources` state carries over on a copy of the live brain (no re-capture storm); disable/enable respected by the tick; per-connector failure isolation test.
- **M2 — App shell.** Provisioner, supervisor, MenuBarExtra, main window + native **Today**, Settings General, login item. *Accept:* clean-machine simulation (fresh `$HOME` sandbox dir): app provisions runtime, boots daemon, Today matches `/api/digest` ground truth; kill -9 the daemon → supervisor restarts it and menu bar shows the blip; ⌘Q leaves no orphan processes.
- **M3 — Migration + MCP proxy.** §10 assistant (states A/B/C), LaunchAgent retirement + rollback script, shims, `brain-mcp` with auto-launch and read-only fallback (+ `read_only` no-event-write mode in service). *Accept:* full State-A migration on Peter's machine passes the 8-point checklist; label detection unit-tested against fixture plists for **both role sets** (incl. `capture-secondary`); §10.1 acceptance run as a **three-home simulation** (one primary, two children: both outboxes ingest under distinct origins, push fan-out leaves both mirrors fresh, pausing one child's `sync:<peer>` job doesn't perturb the other; the real secondary Mac migrates after the primary is stable); force-quit app → Codex `retrieve_context` still answers (read-only) and `propose_memory` fails with the actionable error; rollback script restores launchd operation.
- **M4 — Queue.** Native centerpiece per v2 §3.2 contract + §4.3 mechanics; notifications. *Accept:* 20-item mixed queue triaged keyboard-only; decisions land in ledger/questions/memories tables (verify via API against ground truth); undo within window; conflict side-by-side renders both facts' evidence.
- **M5 — Wiki + Entities.** Native renderer w/ escape+fixture tests, provenance popovers (confirm/flag round-trip), contract rail, snapshot diffs; entity index/detail/merge-propose. *Accept:* the databricks page reads as a document; popover shows verbatim quote + opens source; merge proposal appears as a policy-gated action.
- **M6 — Ask + Ops + Connectors UI + Embeddings manager + web-UI fallback hardening.** §4.3 Ask/Ops; §5.5 cards; §6 stamped indexes incl. live index migration + flip flow; ⌘K + `?`; web UI confirmed **kept** behind `--serve-web` as the maintained platform-portability fallback (off by default, never deleted in this plan); docs updated (README gains an "Install the app" path; this spec re-stamped). *Accept:* negative-control query visibly shows `no_strong_match`; flip to a second model on a temp home → eval comparison shown → rollback is instant; stamped-index migration leaves `brain index doctor` clean on the live brain; the fixture connector proves §5.6 (“new connector = one Python module”); web UI six-view manual pass on a seeded home behind `--serve-web` is green.

Sequencing note: M0–M1 are pure Python and can start immediately; M2 depends on M0; M3 is the switch point that changes Peter's machine — everything before it must not disturb the running launchd setup.

### 14.1 Verification record

- **2026-07-08 M0-M2 audit hardening (`281e5e2`):** added the missing nightly-parity test, run-now-while-paused coverage, per-peer sync cadence override, CLI `brain --version`, supervisor replacement of adopted mismatched daemons, runtime `brain --version` smoke-gating, explicit ad-hoc signing in `scripts/build-app.sh`, and a headless M2 acceptance harness.
- **2026-07-08 gates after M3 (`03a2adf`):** `uv run ruff check .` passed; `uv run pytest -q` passed with 390 tests; `swift test --package-path app` passed with 7 tests; `scripts/m3-migration-acceptance.sh` passed with 12 focused Python tests plus the Swift suite.
- **2026-07-08 M2 clean-machine simulation after M3:** `scripts/m2-clean-machine-acceptance.sh` passed using a fresh temp `$HOME`; it provisioned the app runtime from bundled resources, installed Python 3.13.12 and 68 locked dependencies, launched the daemon, verified Today digest content against `/api/digest`, killed daemon pid `46252`, observed supervisor restart to pid `46262`, and stopped with no supervised daemon left running. Reported result: `runtime_phase=Ready`, `version=0.1.0`, `digest_queue_total=0`, app bundle `/Users/Peter/pkm-brain/app/DerivedData/Build/Products/Release/PKM Brain.app`.
- **2026-07-08 M3 live State-A migration on Peter's primary:** created runtime backup `~/Library/Application Support/PKM Brain/migration/runtime-backups/20260708T191558Z`; retired `com.pkm-brain.agent-log-ingest`, `com.pkm-brain.nightly-maintenance`, and `com.pkm-brain.sync-primary` from `~/Library/LaunchAgents`; wrote backup plists and `rollback.sh` under `~/Library/Application Support/PKM Brain/migration/plists-backup/`; installed app-managed `brain` and `brain-mcp` shims; rewrote Codex and Claude MCP registrations to the shim path; final live app daemon is running from runtime `0.1.0-762bf645-42147f2b` with migration plan state `adopt` and no detected legacy LaunchAgents.
- **2026-07-08 M3 live verification:** final runtime health returned `ok`; final runtime scheduler ran `capture_tick` successfully at `2026-07-08T20:17:56+00:00` and `sync:Peters-Mac-mini` with status `ok` at `2026-07-08T20:19:27+00:00`; hard-killing app pid `45972` caused daemon pid `45998` to exit via the parent monitor, logging `daemon_parent_missing`; the app-managed MCP proxy then returned `retrieve_context` in read-only mode with no retrieval event and declined `propose_memory` with `PKM Brain app is not available; write declined. Launch the app and retry.`

## 15. Hard rules (inherit all standing invariants)

1. Evidence → facts → pages information flow untouched; raw sources immutable; managed pages remain projections.
2. One data model, one approval path: every mutation goes through existing service/CoS/question/memory functions. The app adds **zero** new write paths to knowledge.
3. Memory approval stays human-and-local (native UI is local-human); MCP never exposes approval or wiki mutation.
4. Swift never opens SQLite/LanceDB.
5. Never mix vector spaces: index identity = embedding stamp, enforced by the per-stamp layout.
6. Blast radius: one bad connector/memory/row degrades its own unit to a warning, never the whole run (audit item 3 discipline).
7. Anything Peter's machine depends on gets a rollback path before it gets a migration.
