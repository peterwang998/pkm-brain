# Primary / Secondary Brain Sync Spec

Spec version: 0.1

Status: Draft target design; current implementation status is tracked in Section 19

Last updated: 2026-05-18

## 1. Purpose

PKM Brain should support multiple local machines without exposing any machine to the public internet.

The target deployment is:

- A mobile laptop that moves between networks and owns the canonical Brain.
- A stationary secondary machine that stays isolated on the local network and can run long-running agents.
- Predictable sync when both devices are on the same LAN.
- No public inbound SSH, no public Brain service, and no dependency on cloud storage for private Brain data.

Core principle:

```text
The Primary Brain owns canonical source material.
Secondary Brains produce local source captures and maintain rebuildable mirrors.
SQLite, vector indexes, logs, and caches are local derived state unless explicitly exported as source.
```

## 2. Definitions

Primary Brain:

- The canonical source-of-truth Brain workspace.
- Usually the laptop.
- Owns authoritative source files, reviewed memories, wiki pages, and sync policy.
- Pulls source captures from secondary nodes.
- Pushes canonical source material back to secondary nodes for local mirror rebuilds.

Secondary Brain:

- A non-authoritative local Brain workspace.
- Usually a stationary desktop, server, or mini PC.
- Can run Codex, Claude, OpenCode, Hyprnote capture, scheduled jobs, and local retrieval.
- Produces an outbox of local source captures for the Primary Brain.
- Rebuilds local SQLite, FTS, and vector indexes from mirrored canonical sources.

Source material:

- Durable files that should survive machine loss and be replicated.
- Examples: captured agent session Markdown, normalized raw documents, wiki Markdown, reviewed memory exports, and selected config.

Derived state:

- Machine-local state that can be rebuilt.
- Examples: `db/`, `indexes/`, `logs/`, SQLite WAL/SHM files, caches, shell snapshots, and local agent runtime histories.

## 3. Goals

- Keep the Secondary private to the LAN.
- Let Codex Mobile or remote-controlled workflows run on the Secondary.
- Preserve Secondary agent sessions and fold them back into the laptop's canonical Brain.
- Allow the Secondary to serve as a useful local mirror when the laptop is home.
- Keep sync deterministic and debuggable with plain files, SSH, and `rsync`.
- Validate at install time whether a workspace is Primary or Secondary.
- Validate that configured peer connectivity is live before enabling scheduled sync.
- Provide an optional local-only control plane for status, setup, sync validation, job management, and memory review.
- Avoid bidirectional writes to the same logical source path.

## 4. Non-Goals

- No public Brain server in V1.
- No public Web UI in V1.
- No public inbound SSH to the Secondary.
- No always-on cross-internet mesh requirement.
- No direct syncing of live SQLite or LanceDB directories.
- No automatic conflict merging for the same logical source path in V1.
- No multi-user permissions model.

## 5. Role Model

Each Brain workspace has a node identity and role.

Proposed config file:

```yaml
# ~/brain/config/sync.yaml
node_id: primary-laptop
role: primary
brain_home: ~/brain

peers:
  - node_id: secondary-desktop
    role: secondary
    host: secondary-host.local
    user: remote-user
    brain_home: ~/brain
    transport: ssh
    trust: lan-only
```

Secondary example:

```yaml
# ~/brain/config/sync.yaml
node_id: secondary-desktop
role: secondary
brain_home: ~/brain

primary:
  node_id: primary-laptop
  expected_user: local-user

outbox:
  enabled: true
  path: ~/brain/outbox/secondary-desktop
```

Rules:

- `node_id` must be stable and unique.
- `role` must be exactly `primary` or `secondary`.
- A Primary may define multiple Secondary peers.
- A Secondary should define exactly one Primary.
- Only the Primary can mark synced source material as canonical.

## 6. Filesystem Layout

Primary workspace:

```text
~/brain/
  inbox/
    external/
      <secondary-node-id>/
        agent_logs/
        documents/
  raw/
  wiki/
  memory/
  config/
  db/          local only
  indexes/     local only
  logs/        local only
```

Secondary workspace:

```text
~/brain/
  inbox/
    agent_logs/
    documents/
  outbox/
    <secondary-node-id>/
      agent_logs/
      documents/
      manifest.jsonl
  raw/         mirror from primary or local rebuild source
  wiki/        mirror from primary
  memory/      mirror from primary
  config/
  db/          local only
  indexes/     local only
  logs/        local only
```

The secondary `outbox/<node_id>/` is append/update-oriented source export. The primary imports it under `inbox/external/<node_id>/`.

## 7. Data Ownership

Primary-owned canonical paths:

```text
raw/
wiki/
memory/
config/shared/
inbox/external/* after pull
```

Secondary-owned source paths:

```text
outbox/<secondary-node-id>/
inbox/agent_logs/ before export
inbox/documents/ before export
```

Never sync as source-of-truth:

```text
db/
indexes/
logs/
*.sqlite
*.sqlite-wal
*.sqlite-shm
.DS_Store
cache/
tmp/
```

Config split:

- `config/shared/` may be mirrored from Primary to Secondary.
- `config/local/` stays machine-local.
- `config/sync.yaml` is local role configuration and should not be blindly overwritten.

## 8. Sync Direction

Primary pull from Secondary:

```text
secondary:~/brain/outbox/<secondary-node-id>/
  -> primary:~/brain/inbox/external/<secondary-node-id>/
  -> primary: brain ingest
```

Primary push to Secondary:

```text
primary canonical source dirs
  -> secondary mirror source dirs
  -> secondary: brain ingest
```

The Primary initiates both pull and push over SSH when it is on the LAN. The Secondary does not need public inbound access from outside the home network.

## 9. Agent Session Capture Behavior

On Secondary:

```text
Codex / Claude / OpenCode local session stores
  -> brain capture agents --export-outbox
  -> ~/brain/outbox/<secondary-node-id>/agent_logs/<agent>/<session_id>.md
```

On Primary:

```text
rsync pull
  -> ~/brain/inbox/external/<secondary-node-id>/agent_logs/<agent>/<session_id>.md
  -> brain ingest
  -> canonical raw/SQLite/wiki/memory/indexes
```

The existing latest-snapshot retention rule applies after import:

- The Primary retains only the latest captured snapshot for each logical session source path.
- Secondary-origin paths include the secondary node namespace to avoid collisions with laptop sessions.
- A Secondary Codex session and a Primary Codex session with the same session ID must still have different logical source paths because the node namespace differs.

## 10. Build Plan

### 10.1 Config Model

Add a sync config parser:

```text
src/pkm_brain/sync_config.py
```

Responsibilities:

- Load `~/brain/config/sync.yaml`.
- Validate role-specific required fields.
- Normalize host/user/path values.
- Expose typed `PrimaryConfig`, `SecondaryConfig`, and `PeerConfig`.
- Refuse unknown roles.
- Refuse missing or duplicate `node_id` values.

### 10.2 CLI Commands

Add a `brain sync` command group:

```bash
brain sync init-primary
brain sync init-secondary
brain sync add-peer
brain sync doctor
brain sync test-connection <secondary-node-id>
brain sync pull <secondary-node-id>
brain sync push <secondary-node-id>
brain sync run <secondary-node-id>
brain sync status
```

Command meanings:

- `init-primary`: writes primary role config and creates required source directories.
- `init-secondary`: writes secondary role config and creates outbox directories.
- `add-peer`: adds or updates a secondary peer in primary config.
- `doctor`: validates local role config and directory layout.
- `test-connection`: validates SSH, remote Brain path, remote role config, and remote node identity.
- `pull`: pulls secondary outbox into primary external inbox.
- `push`: pushes canonical source material to secondary.
- `run`: executes pull, primary ingest, push, and remote secondary ingest.
- `status`: reports last sync times, peer reachability, and pending outbox file counts.

All setup commands must be interactive by default. Flags are optional for scripts and CI, but a human install should be able to run the commands with no arguments and answer prompts.

`brain sync init-primary` prompts for:

- Primary `node_id`.
- Brain home path, defaulting to `~/brain`.
- Whether to add a Secondary peer now.
- Optional shared config directory.
- Whether to install the Primary scheduled sync job after validation.

`brain sync init-secondary` prompts for:

- Secondary `node_id`.
- Brain home path, defaulting to `~/brain`.
- Expected Primary `node_id`.
- Outbox path, defaulting to `~/brain/outbox/<node_id>`.
- Which local capture sources to enable.
- Whether to install the Secondary scheduled capture job after validation.

`brain sync add-peer` prompts for:

- Secondary `node_id`.
- SSH host or LAN IP.
- SSH username.
- Remote Brain home path.
- Optional SSH identity file.
- Whether to trust the first observed host key.
- Whether to run `brain sync test-connection` immediately.

Non-interactive form:

```bash
brain sync init-primary --node-id <primary-node-id> --home <primary-brain-home> --yes
brain sync init-secondary --node-id <secondary-node-id> --primary-node-id <primary-node-id> --home <secondary-brain-home> --yes
brain sync add-peer --node-id <secondary-node-id> --host <secondary-host> --user <ssh-user> --brain-home <secondary-brain-home> --yes
```

### 10.3 Main Installer Integration

The main Brain installation process must expose this topology directly instead of requiring users to discover `brain sync` separately.

Add an interactive setup wizard:

```bash
brain setup
```

or an explicit wizard mode on init:

```bash
brain init --wizard
```

The wizard should perform normal single-machine Brain setup first, then ask whether to configure multi-device sync.

Required wizard flow:

```text
1. Choose Brain home path.
2. Initialize or validate the local Brain workspace.
3. Ask: "Set up multi-device Primary/Secondary sync now?"
4. If no, finish normal local setup.
5. If yes, ask whether this machine is Primary or Secondary.
6. If Primary:
   - Prompt for Primary node ID.
   - Ask whether to add a Secondary now.
   - If yes, prompt for Secondary node ID, SSH host, SSH user, remote Brain home, and optional SSH identity file.
   - Run `brain sync doctor`.
   - Run `brain sync test-connection <secondary-node-id>`.
   - Offer to install the Primary scheduled sync job only if validation passes.
7. If Secondary:
   - Prompt for Secondary node ID.
   - Prompt for expected Primary node ID.
   - Prompt for outbox path.
   - Prompt for local capture sources to enable.
   - Run `brain sync doctor`.
   - Offer to install the Secondary scheduled capture job only if validation passes.
```

The wizard must support `--dry-run` and `--json` modes so users can preview the planned config without writing files. Non-interactive flags may exist for automation, but the default user path should be guided prompts.

The wizard must not ask for or persist private key contents, tokens, passwords, or other secrets. It may store paths to SSH identity files and trusted host-key fingerprints.

### 10.4 Capture Export

Extend capture:

```bash
brain capture agents --export-outbox
```

Behavior on a Secondary:

- Capture local agent logs as usual.
- Write or copy captured Markdown into `outbox/<node_id>/agent_logs/<agent>/`.
- Update `outbox/<node_id>/manifest.jsonl`.
- Do not mark the outbox as canonical.

Manifest row shape:

```json
{
  "node_id": "secondary-desktop",
  "source_kind": "agent_session_log",
  "agent": "codex",
  "session_id": "019e...",
  "relative_path": "agent_logs/codex/019e....md",
  "content_hash": "sha256...",
  "captured_at": "2026-05-18T12:00:00Z",
  "source_path": "<local-agent-session-path>"
}
```

### 10.5 Rsync Transport

Use `rsync` over SSH for V1.

Pull command shape:

```bash
rsync -az --delete \
  <ssh-user>@<secondary-host>:<secondary-brain-home>/outbox/<secondary-node-id>/ \
  <primary-brain-home>/inbox/external/<secondary-node-id>/
```

Push command shape:

```bash
rsync -az --delete \
  --exclude 'db/' \
  --exclude 'indexes/' \
  --exclude 'logs/' \
  --exclude '*.sqlite*' \
  <primary-brain-home>/raw/ \
  <ssh-user>@<secondary-host>:<secondary-brain-home>/raw/
```

Push should separately mirror:

```text
raw/
wiki/
memory/
config/shared/
```

It should not overwrite:

```text
config/sync.yaml
config/local/
outbox/
db/
indexes/
logs/
```

### 10.6 Remote Commands

After push, the Primary may ask the Secondary to rebuild derived state:

```bash
ssh <ssh-user>@<secondary-host> 'cd <pkm-brain-repo> && uv run brain ingest --home <secondary-brain-home>'
```

This command is LAN-only and initiated by the Primary.

### 10.7 Scheduling

Primary scheduled sync job:

```text
com.pkm-brain.sync-primary
StartInterval = 1800
command = brain sync run <secondary-node-id> --if-reachable
```

Secondary scheduled capture job:

```text
com.pkm-brain.capture-secondary
StartInterval = 600
command = brain automation secondary-tick
```

The initial implementation may render these as macOS LaunchAgents. The scheduler interface should not assume launchd-only semantics because Linux support should map the same logical jobs to systemd user timers, and generic Unix support should be able to fall back to cron or on-demand commands.

Add a logical scheduler command group:

```bash
brain scheduler install-sync --peer <secondary-node-id> --interval 1800
brain scheduler install-secondary-capture --interval 600
brain scheduler status
brain scheduler uninstall-sync
brain scheduler uninstall-secondary-capture
```

V1 uses a single fixed Primary sync LaunchAgent label, so `uninstall-sync` does
not accept a peer argument. Multi-Secondary per-peer scheduling labels are a
post-V1 extension.

macOS-specific `brain launch-agent ...` commands may remain as lower-level adapter commands, but user-facing setup and Web UI flows should prefer the scheduler abstraction.

Failure behavior:

- If the Secondary is unreachable, Primary sync exits success with `reachable=false`.
- If SSH works but remote validation fails, sync exits failure.
- If pull succeeds but ingest fails, do not push back to Secondary.
- If push succeeds but remote ingest fails, report degraded mirror.

## 11. Install-Time Validation

Installation must validate local role and peer connectivity before enabling scheduled sync.

The preferred install path is the main setup wizard. The lower-level commands below are the underlying operations that the wizard calls and are also available for scripted setup.

Primary install flow:

```bash
brain sync init-primary
brain sync add-peer
brain sync doctor
brain sync test-connection <secondary-node-id>
brain scheduler install-sync --peer <secondary-node-id> --interval 1800
```

Secondary install flow:

```bash
brain sync init-secondary
brain sync doctor
brain scheduler install-secondary-capture --interval 600
```

`brain scheduler` is the logical command group for scheduled Brain jobs. The macOS implementation may render LaunchAgents; Linux may render systemd user timers; generic Unix may render cron entries or report that only on-demand execution is available.

During interactive install, `init-primary`, `init-secondary`, and `add-peer` must never print or persist secrets such as private key material. They may persist paths to key files and trusted host-key fingerprints.

`brain sync doctor` must check:

- `sync.yaml` exists.
- `node_id` exists and is stable.
- `role` is valid.
- Required role-specific fields exist.
- Required directories exist or can be created.
- Local Brain home matches configured `brain_home`.
- `db/`, `indexes/`, and `logs/` are marked local-only.
- No sync config attempts to mirror SQLite or LanceDB paths.

`brain sync test-connection <peer>` from Primary must check:

- Host resolves or IP is syntactically valid.
- SSH connects without interactive prompts.
- Remote command execution works.
- Remote `brain` executable is available.
- Remote `brain sync doctor --json` succeeds.
- Remote `node_id` matches the configured peer.
- Remote `role` is `secondary`.
- Remote `brain_home` matches the configured path.
- Remote outbox exists or can be created.
- A small write/read/delete probe succeeds in the remote outbox.
- `rsync --version` exists locally and remotely.

Secondary validation must check:

- It is not configured as Primary unless explicitly reinitialized.
- Its outbox path includes its own `node_id`.
- It has local capture permissions for configured agents.
- It does not attempt to push canonical source material upstream by itself.

## 12. Connection Test Output

Human output:

```text
Role: primary
Node: primary-laptop
Peer: secondary-desktop
SSH: ok
Remote brain: ok
Remote role: secondary
Remote node id: secondary-desktop
Remote outbox write probe: ok
Rsync: ok
Ready for scheduled sync: yes
```

JSON output:

```json
{
  "local_role": "primary",
  "local_node_id": "primary-laptop",
  "peer_node_id": "secondary-desktop",
  "checks": {
    "ssh": "ok",
    "remote_brain": "ok",
    "remote_role": "ok",
    "remote_outbox_probe": "ok",
    "rsync": "ok"
  },
  "ready": true
}
```

## 13. Conflict Policy

V1 avoids conflicts by construction.

Rules:

- The Primary owns canonical `raw/`, `wiki/`, and `memory/`.
- Each Secondary writes only under `outbox/<node_id>/`.
- Primary imports Secondary outboxes under `inbox/external/<node_id>/`.
- Logical source identity includes `node_id`, `source_kind`, agent, and session ID.
- If two nodes produce the same path unexpectedly, Primary preserves both under node-specific namespaces and reports a warning.

Manual review command:

```bash
brain sync conflicts
```

V1 conflict output should be advisory only.

## 14. Security Model

The Secondary remains LAN-only.

Security constraints:

- No public port forwarding.
- No cloud relay required.
- Primary initiates SSH when local network reachability exists.
- SSH keys should be restricted to the user account and not reused for unrelated services.
- Remote validation commands must avoid printing secrets.
- Sync logs should include paths, hashes, counts, and status, not raw document contents.

Optional hardening:

- Use a dedicated SSH key for Brain sync.
- Restrict the Secondary account command surface with a forced command wrapper.
- Add host-key pinning to `sync.yaml`.
- Require `--allow-first-host-key` for initial trust and refuse changed host keys later.

## 15. Observability

Add sync run records to SQLite:

```sql
sync_runs(
  id TEXT PRIMARY KEY,
  peer_node_id TEXT NOT NULL,
  direction TEXT NOT NULL,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  status TEXT NOT NULL,
  files_pulled INTEGER NOT NULL DEFAULT 0,
  files_pushed INTEGER NOT NULL DEFAULT 0,
  bytes_pulled INTEGER NOT NULL DEFAULT 0,
  bytes_pushed INTEGER NOT NULL DEFAULT 0,
  primary_ingest_run_id TEXT,
  remote_ingest_status TEXT,
  errors TEXT NOT NULL DEFAULT '[]'
)
```

`brain sync status` should show:

- Last successful pull per peer.
- Last successful push per peer.
- Last failed run and error summary.
- Pending secondary outbox count if reachable.
- Whether Primary and Secondary have matching latest canonical manifest hash.

## 16. Local Web UI Control Plane

The sync topology should expose a lightweight local Web UI as an optional control plane. The Web UI is not a public Brain service and must not become a second source of truth.

Default command:

```bash
brain ui --host 127.0.0.1 --port 8765
```

Default access:

```text
http://127.0.0.1:8765
```

Default behavior:

- Bind to `127.0.0.1` only.
- Start on demand and stop when the user exits the command.
- Use a local token or equivalent local authentication gate.
- Avoid writing raw document contents into server logs.
- Keep polling bounded so idle CPU is effectively zero and idle memory stays modest.

Reserved post-V1 always-on service commands:

```bash
brain ui service install
brain ui service status
brain ui service uninstall
```

Always-on mode is optional and not required for V1. A laptop Primary should work
well with on-demand UI startup. A stationary Secondary may later use an always-on
local service, still bound to loopback.

Access modes:

- Same-machine access: run `brain ui` on the machine and open `http://127.0.0.1:8765`.
- Secondary access from the Primary laptop: keep the Secondary UI loopback-only and use SSH port forwarding.

```bash
ssh -L 8765:127.0.0.1:8765 <ssh-user>@<secondary-host>
```

The browser still opens `http://127.0.0.1:8765` on the laptop; traffic reaches the Secondary only through SSH.

LAN-visible mode is not default. If supported, it must require explicit flags, authentication, and a warning:

```bash
brain ui --host 0.0.0.0 --port 8765
```

Required UI areas:

- Status: Brain home, role, node ID, database/index size, last ingest, last nightly run, last sync, and current warnings.
- Setup wizard: normal single-machine setup, optional Primary/Secondary sync setup, peer connection testing, and scheduled job installation.
- Sync: configured peers, reachability, dry-run pull/push, last run details, pending outbox counts, and conflict warnings.
- Jobs: install, uninstall, status, and test actions for supported background schedulers.
- Logs: recent capture, ingest, sync, nightly maintenance, and Web UI errors.
- Memory review: proposed, active, rejected, and archived memories with source evidence and review actions.

Memory review behavior:

- The Web UI must call the same service-layer operations as the CLI-equivalent memory commands.
- It may approve, reject with reason, archive, and inspect memories.
- It must show source IDs, confidence, scope, memory type, status, reviewed timestamp, and review reason.
- It should surface mechanical validation from `brain memory audit`.
- It should flag missing sources, low confidence, likely duplicates, stale claims, and possible conflicts when those checks exist.
- It must treat `active` memories as reviewed guidance and `proposed` memories as unreviewed candidates.
- MCP agents may propose memories, but memory approval remains a local human action through CLI or local UI.

Implementation boundary:

- The Web UI should be a thin layer over existing Brain service functions and JSON-style status endpoints.
- It should not create a separate data model, separate memory store, or separate sync engine.
- Initial endpoints should mirror existing CLI concepts: doctor, index status, memory list/inspect/approve/reject/archive/audit, launch job status, capture, ingest, sync status, and sync conflicts.
- The Web UI should be platform-neutral. Background service installation should use an adapter layer:
  - macOS: LaunchAgent.
  - Linux: systemd user service/timer.
  - Generic Unix fallback: cron or on-demand only.

## 17. Test Plan

Unit tests:

- Primary config validates with peer entries.
- Secondary config validates with one Primary reference.
- Missing role fails validation.
- Invalid role fails validation.
- Duplicate peer node IDs fail validation.
- Local-only paths cannot appear in mirror path config.
- Secondary outbox path must include `node_id`.
- Rsync command builder excludes `db/`, `indexes/`, `logs/`, and SQLite files.

Integration tests with a local temp SSH substitute or fake transport:

- Primary pulls secondary outbox into `inbox/external/<node_id>/`.
- Primary ingest treats pulled agent logs as `agent_session_log`.
- Latest-snapshot retention works for secondary-origin session logs.
- Primary push mirrors `raw/`, `wiki/`, `memory/`, and `config/shared/`.
- Secondary rebuild command is invoked only after successful Primary ingest.
- Unreachable peer with `--if-reachable` exits cleanly and records skipped status.
- Remote role mismatch fails connection validation.
- Remote node ID mismatch fails connection validation.

Manual acceptance test:

1. Configure the laptop as Primary.
2. Configure a LAN-only secondary machine as Secondary.
3. Run `brain sync test-connection <secondary-node-id>`.
4. Start a Codex run on the Secondary, including a Codex Mobile-triggered run.
5. Run secondary capture.
6. Run primary sync.
7. Confirm the Primary has the Secondary session in `inbox/external/<secondary-node-id>/`.
8. Run primary ingest.
9. Confirm retrieval can find the Secondary session.
10. Push canonical source material back to the Secondary.
11. Confirm Secondary local retrieval sees the same source after rebuild.

## 18. Implementation Decisions And Deferred Questions

- Reviewed memories are exported as Markdown files under `memory/` and imported by `memory_id`. SQLite remains the canonical local store.
- Primary sync imports captured Secondary Markdown from the outbox. Structured `agent_sessions` record sync is deferred to post-V1.
- Remote ingest runs after each successful push.
- Secondary read-only MCP mode is deferred. The normal local MCP server can read the Secondary's local mirror.
- Peer configuration accepts hostnames or static LAN IPs.
- V1 Web UI uses a framework-free stdlib HTTP server rather than FastAPI. This keeps dependencies small; typed schemas/OpenAPI would require a future port.
- V1 Web UI is on-demand only. `brain ui service install/status/uninstall` is reserved for post-V1.
- V1 scheduler supports one fixed Primary sync LaunchAgent label. Multi-Secondary per-peer scheduler labels are deferred.

## 19. Implementation Status And Drift Audit

Audit date: 2026-05-19.

This spec began as a target design. The codebase now implements the V1
Primary/Secondary sync path through the setup wizard, local UI, scheduler
adapter, and sync transport. The remaining work before tagging V1 is real
two-machine acceptance.

Currently implemented in code:

- Direct workspace initialization through `brain init`.
- Guided setup through `brain setup` and `brain init --wizard`.
- General `brain doctor`, ingest, search, retrieve-context, MCP server, and index status commands.
- Agent capture for Codex, Claude, OpenCode, and opt-in Hyprnote into the local inbox.
- Origin-aware latest-snapshot retention for re-ingested `agent_session_log` source paths.
- Memory proposal, list, inspect, approve, reject, archive, and audit commands.
- Memory export/import under `memory/`.
- Retrieval contract that separates reviewed `active_memories` from unreviewed `candidate_memories`.
- MCP memory proposal and memory retrieval with status filtering.
- macOS LaunchAgent installation, status, rendering, and uninstall for local capture and nightly maintenance.
- Nightly maintenance with optional wiki proposals and optional failure-memory proposals.
- `config/shared/` and `config/local/` split, with a deprecation shim for legacy `config/config.yaml`.
- `sync_config.py` and `config/sync.yaml` role validation.
- Primary/Secondary role initialization.
- SSH peer validation and host-key pinning.
- Pure `rsync` pull/push command builders.
- Secondary outbox export with `manifest.jsonl`.
- Staged Primary pull into `inbox/external/<secondary-node-id>/`, manifest hash verification, rejection/quarantine paths, and ingest orchestration.
- Primary push of `raw/`, `wiki/`, `memory/`, and `config/shared/`, followed by remote Secondary ingest.
- `sync_runs` SQLite table, `brain sync status`, and advisory `brain sync conflicts`.
- `brain scheduler` command group with macOS LaunchAgent jobs for Primary sync and Secondary capture.
- `brain ui` with loopback binding, local auth token, JSON API, and browser pages for Status, Setup, Sync, Jobs, Logs, and Memory Review.

Deferred or post-V1:

- Manual two-machine acceptance is still required before tagging V1.
- `brain ui service install/status/uninstall`.
- Real systemd user timer and cron implementations. V1 non-macOS adapters intentionally raise a clear not-yet-implemented error.
- Structured `agent_sessions` import from Secondary.
- Secondary read-only MCP mode.
- Multi-Secondary per-peer scheduler labels.

Spec drift notes:

- The target scheduling model remains platform-neutral, but V1 implementation support is macOS LaunchAgent only.
- The implemented Web UI uses stdlib `http.server`, not FastAPI. This is an intentional V1 dependency tradeoff.
- The current memory audit is basic: it validates type, status, scope, source presence, and confidence. Duplicate, stale, conflict, and broken-source validation remain future checks for the Web UI to surface.

## 20. Implementation Order

1. Add shared status/service helpers that expose doctor, ingest, index, job, and memory-review state as JSON-safe objects.
2. Add the main `brain setup` or `brain init --wizard` installer flow.
3. Add local Web UI foundation with loopback binding, local auth, status page, and memory review over existing service methods.
4. Add sync config model and `brain sync doctor`.
5. Add role initialization commands.
6. Add peer connection test over SSH.
7. Add rsync command builder and dry-run mode.
8. Add Secondary outbox export for captured agent logs.
9. Add Primary pull into `inbox/external/<node_id>/`.
10. Add Primary push of canonical source directories.
11. Add remote Secondary ingest command.
12. Add sync run logging and status.
13. Add scheduler adapter abstraction with macOS LaunchAgent support first, then systemd user timers and cron/on-demand fallback.
14. Add Web UI setup/sync/job management pages over the same lower-level commands.
15. Add docs to README install flow.
