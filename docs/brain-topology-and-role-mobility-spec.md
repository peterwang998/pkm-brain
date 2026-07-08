# Brain Topology & Role Mobility — Spec

**Status:** design spec for Codex — extends `docs/primary-secondary-brain-sync-spec.md` (which stays authoritative for the V1 single-primary/single-secondary transport) and companions `docs/macos-app-spec.md` (§3.2, §10.1, §11 carry the app-side hooks)
**Last verified:** 2026-07-08 against commit `03a2adf` for the M3 three-home multi-child acceptance; earlier code claims verified in `sync_config.py`, `sync_rsync.py`, `sync_transfer.py`, `capture.py`, `paths.py`, `service.py`
**Author:** Claude, from Peter's questions (multi-child, primary migration/demotion, work-vs-personal isolation)

Scope: three extensions to the topology model, in dependency order —

1. **Multi-child**: one primary, N children (§2).
2. **Prerequisites** for anything that moves state between machines: home-relative paths, snapshot replication, a shared topology record (§3).
3. **Role mobility**: planned primary handover, disaster promotion, demotion (§4).
4. **Profiles**: multiple fully isolated brains (work vs personal) coexisting on one device, including colocated children of different brains (§5).

The one invariant everything below protects: **exactly one writer per logical path, and at most one primary per brain, at all times — including during transitions.** Role mobility moves the writer; it never adds one.

---

## 1. Topology model & terminology

- A **brain** is a tree of exactly one **primary** and zero or more **children** (the code/config term remains `secondary`; "child" is UI language). Star only: children never sync with each other, no child-of-child, no mesh, no leader election, no automatic failover — ever. Promotion is a human act in a personal system.
- A **device** may host multiple **brain homes** (profiles, §5). Node identity is per-home (`config/local/node_id`), not per-device — this is what makes colocated peers possible.
- A **profile** is the app-level handle for one brain home on one device (registry, daemon, UI tint). Same thing the core calls a home.

## 2. Multi-child (closes the sync spec's deferred item)

Verified foundation: `sync_config.py` models `peers` as a validated list with unique node_ids; `brain sync run <peer>` is peer-scoped; staging (`inbox/external/<node-id>/`), outboxes (`outbox/<node-id>/`), and origin identity (`origin_node_id` + `logical_source_key`) are all per-node. **N children introduce no new conflict classes** — children never write canonical paths, and origins cannot collide. The only real gap was launchd's one-plist-per-peer scheduling, which the app daemon's job registry dissolves.

Requirements (app-spec §3.2/§4.3/§10.1/M3 carry the implementation detail):

- **R2.1** One `sync:<peer-node-id>` scheduler job per roster entry, independent cadence/pause/status. Serial executor in v1.
- **R2.2** Per-peer Ops matrix on the primary: reachable, last pull, last push, mirror freshness, outbox depth at last contact.
- **R2.3** Add-child wizard on the primary wrapping `add-peer` → `test-connection` → `acceptance`; removing a child archives (not deletes) its `inbox/external/<node-id>/` staging history.
- **R2.4** `sync_runs` rows must be attributable per peer (verify; add a peer column if absent).
- **Acceptance:** three-home simulation (1 primary + 2 children): both outboxes ingest under distinct origins; push fan-out leaves both mirrors fresh; pausing one child's job doesn't perturb the other; per-peer matrix matches ground truth.

## 3. Prerequisites (independently valuable; do these before §4)

### 3.1 P1 — Home-relative paths in SQLite

**Problem (verified):** `documents.raw_path` is stored absolute (`str(raw_path)`, `service.py:1222`); `wiki_pages.path` rows observed absolute on the live brain. A DB restored under a different `$HOME`/username (new laptop, promoted child) breaks every pointer — this is the sync spec's long-standing worry ("absolute paths from the wrong machine inside SQLite rows") made concrete.
**Fix:** audit every column that stores filesystem paths (`documents.raw_path`/`source_path`, `wiki_pages.path`, `capture_sources`, `wiki_page_snapshots`, `agent_sessions`, others found by grep); migrate to **home-relative** storage with a single resolve helper on read; write-side stores relative from then on. Ship `brain db rebase-paths --from <prefix> --to <prefix>` as the fallback for old snapshots.
**Acceptance:** a live-brain copy relocated to a different `$HOME` in a test sandbox passes `doctor`, `provenance check`, search, and raw-context resolution with zero path errors.

### 3.2 P2 — DB snapshot replication (also your off-machine backup)

**Problem:** the mirror (`raw/`, `wiki/`, `memory/`, `config/shared/`) is *not* the whole primary. Primary-only SQLite state not derivable from mirrored files: the CoS action ledger (audit trail + inverses), policy versions and autonomy state, open questions + human answers, fact confirmations, the entity registry, capture/sync watermarks. Facts are regenerable from raw (`rebuild-facts`) but that is expensive LLM work and loses human decisions.
**Fix:** a nightly `db_snapshot_export` stage on the primary: cold, checkpointed copy (`VACUUM INTO` a temp file under a paused writer window in the serial executor), gzip, stamp with `{node_id, primary_epoch, created_at, schema_version}`, write to `~/brain/snapshots/db/` (a reserved, never-ingested path), include in the push set. Children retain the last K=3 under their mirror. This does not violate "never sync live SQLite" — that rule exists because of WAL-in-flight and concurrent writers; these snapshots are cold and single-writer by construction. Snapshot size tracks the DB; audit item 5's telemetry compaction directly shrinks it.
**Acceptance:** snapshot restore drill — restore the replicated snapshot on a second home, rebase paths (P1), `doctor` + `provenance check` clean, counts match the manifest.

### 3.3 P3 — Shared topology record

**Problem:** the peer roster lives only in the primary's local `sync.yaml`; a promoted child wouldn't know who its siblings are. There is also no shared statement of "who is primary."
**Fix:** primary-authored `config/shared/topology.json`: `{brain_id, primary_node_id, primary_epoch, children: [{node_id, host, user, brain_home}], updated_at}`. It rides the existing `config/shared/` push, so every child mirrors it. `brain_id` is a stable UUID minted at init — profiles (§5) and pairing use it to prevent cross-brain grafts.
**Acceptance:** after a normal sync, every child's mirrored `topology.json` matches the primary's; Ops on every node displays "primary = X @ epoch N".

## 4. Role mobility

### 4.1 Epoch fencing (the split-brain guard)

`primary_epoch` is a monotonic integer, incremented at **every** role transition. Children persist the highest `(primary_node_id, epoch)` they have accepted. Enforcement:

- Every sync session begins with an epoch exchange: the primary presents `{node_id, epoch}`; the child-side remote command refuses the session if the presented epoch is lower than its recorded one, records the refusal, and both sides' apps raise an alert ("this node believes primary is Y @ N+1; you are X @ N — demote or investigate").
- v1 enforcement point is the child-side `brain sync` remote commands (remote ingest, `rebuild-mirror-index`), which the primary invokes over SSH on every push — a stale primary is refused before any child-side state is rebuilt. Hardening (deferred, tracked): epoch-checked **staged push** (`sync begin` → rsync to child-side staging → `sync commit` validates epoch → promote to mirror), closing the residual window where rsync could land files in the mirror before refusal. The mirror is derived state, so the interim risk is recoverable, not corrupting.
- Every node's Ops shows its `(primary_node_id, epoch)` belief at all times.

### 4.2 Planned handover (both machines alive — the normal path, e.g. new laptop)

Assistant flow "Hand over primary to <child C>", each step idempotent, dry-run first:

1. **Preflight:** P1–P3 landed; C's mirror healthy; SSH both directions verified; fresh runtime backup; no rebuild/regeneration in flight.
2. **Freeze:** pause schedulers on old primary O and C (drain the running job; other children may keep capturing — their outboxes queue).
3. **Final collect:** O pulls every reachable child outbox → final ingest.
4. **Final publish:** O pushes canonical set + a fresh cold snapshot (P2) to C.
5. **Verify on C:** mirror hash matches; snapshot restores into staging; path rebase (P1); `doctor`, `provenance check`, row counts vs manifest.
6. **Flip:** O writes `role: secondary` + a handover record; C promotes the staged DB to live, writes `role: primary`, `primary_epoch: N+1`, and a new `topology.json` (roster = old roster − C + O-as-child with a fresh `outbox/<O-node-id>/`).
7. **Re-point children:** C contacts each child over already-pinned SSH; the child accepts the role change only with epoch N+1 from a pinned key (or explicit human confirm in that child's app), updates its `primary:` block. O rebuilds its local DB/indexes as a mirror-derived child (its previous live DB is renamed and retained as a timestamped archive, not deleted).
8. **Resume & verify:** C's daemon starts per-peer sync jobs; every child receives a push @ N+1; **fencing test:** O attempting a push @ N is refused. Checklist green → done.

**Rollback:** before step 6, unpause — nothing changed. After step 6, rollback = run the same protocol in reverse (O is just another promotable child now), producing epoch N+2. Epochs only ever go up.

### 4.3 Disaster promotion (primary dead or unavailable)

Assistant flow "Promote this Mac to primary" on child C, with typed confirmation:

1. App computes and displays the **data-loss window** honestly: age of the newest replicated snapshot, age of the last received push, and un-pulled outbox state ("changes made on the old primary after <timestamp> are not in this snapshot").
2. Restore newest snapshot to staging → rebase paths → `doctor`/`provenance`.
3. **Absorb:** ingest C's own outbox and every reachable sibling's outbox (children retain outboxes until a primary imports them, so nothing queued is lost).
4. Write `role: primary`, `primary_epoch: N_seen + 1`, `topology.json` from the mirrored roster (may be stale — human reviews it).
5. Re-point reachable children; unreachable ones fence automatically on next contact.
6. **If the old primary returns:** children refuse it (epoch), its app alerts and offers guided demotion. Salvage on demotion: its inbox/raw material captured after the snapshot is exported through its new outbox and re-ingested by the new primary; **its ledger delta (actions/questions/confirmations after the snapshot) is lost** — stated plainly in the demotion UI. This is why planned handover is the recommended path whenever both machines are alive.

### 4.4 Demotion (standalone)

"Demote to child" on a primary = steps 6–8 of §4.2 with the target primary chosen by the human (used when the *other* machine was already promoted, or to fold a former primary into an existing brain). Requires the new primary's epoch to be higher than the demoting node's.

### 4.5 Non-goals

No automatic failover, no quorum/consensus, no multi-primary, no merge of two divergent primaries' ledgers (if both wrote as primary during a split, the human picks the winner; the loser's delta is salvage-exported evidence, not merged state), no cross-internet handover in v1 (LAN/SSH reachability assumed).

## 5. Profiles — multiple isolated brains on one device

Use case: a **work brain** and a **personal brain** with a hard boundary, possibly both present on the same Mac — including the case where one device hosts a *child of the work brain* and a *child of the personal brain* simultaneously.

### 5.1 What is already isolated (verified)

Everything the core touches is home-scoped: SQLite, LanceDB stamps, raw/wiki/memory/inbox/outbox, `config/` (including `node_id`, sync role, tokens), logs, evals, backups, the daemon handshake + single-instance lock (per-home by design in the app spec), and the whole CLI/MCP surface via `--home`. Two daemons on one Mac already coexist (ephemeral ports, per-home handshakes). Colocated children of different brains work at the transport level today: peer `brain_home` is honored in rsync target paths and in remote commands (`sync_rsync.py:47-102`, `sync_transfer.py:275` — `brain sync rebuild-mirror-index --home <peer.brain_home>`).

### 5.2 The four leak/collision points (verified) and their fixes

1. **Capture double-ingest — the real isolation problem.** Agent session stores (`~/.codex/state_5.sqlite`, `~/.claude/projects`, OpenCode's DB, Hyprnote) are **device-global**. Two profiles with the same connectors enabled would each ingest *every* session: work sessions in the personal brain and vice versa, twice the storage, and a genuine privacy failure.
   **Fix — device-source claims + routing rules** in the connector layer:
   - A **device-source claim registry** shared across profiles at `~/Library/Application Support/PKM Brain/device-sources.json`: each device-global source (`codex`, `claude`, `opencode`, `hyprnote`, future `email:<account>`) is either `exclusive` to one profile (default: the first profile that enables it; the app blocks enabling it elsewhere without changing the claim) or `filtered` with routing rules.
   - **Routing rules** partition by session working directory, which every agent adapter already captures: Codex `cwd` (from the state DB, `capture.py:237,263`), Claude `cwd`/`project` (`capture.py:307`), OpenCode `worktree`/`project_name` (`capture.py:353`). Rules are ordered path prefixes → profile (e.g. `~/work/** → work`, everything else → personal). Deterministic, auditable: each capture tick reports `{captured, routed_elsewhere, unmatched}` per connector; `unmatched` goes to the rule set's declared default profile, never silently to both.
   - **Hyprnote** (meetings) has no cwd: exclusive claim only in v1. Calendar-based routing is future work. The **email** connector routes naturally per account: an account belongs to exactly one profile.
   - Enforcement lives in the shared connector layer (each daemon checks claims before capturing), and the registry file is the coordination point — no daemon-to-daemon communication needed.
2. **MCP registration ambiguity.** Agents register MCP servers globally; with two brains, "pkm-brain" is ambiguous, and `write_agent_session`/`propose_memory` land wherever the agent happened to connect.
   **Fix:** per-profile registrations — `pkm-brain-work` / `pkm-brain-personal` → `brain-mcp --home <home>`; the brain-memory skill gains a routing paragraph ("pick the brain matching the task's context; when unsure, ask"). Also honor a `PKM_BRAIN_HOME` env var in the CLI/MCP shims so project-scoped agent configs (e.g. a work repo's `.mcp.json`) can pin the right brain per project — that's the strongest practical routing.
   **Residual risk, stated honestly:** an agent with both servers registered *can* retrieve from one brain and write a derivative into the other. v1 mitigation is naming + skill policy + per-project pinning, not technical enforcement; a cross-profile taint control would require content-level labeling that contradicts the local-first simplicity. Peter should treat "agent has both brains mounted" as a deliberate choice per tool.
3. **`node_id` hostname collision.** `local_node_id()` falls back to the hostname (`paths.py:114-120`); two homes on one device would present identical node identities to their (different) primaries — confusing at best.
   **Fix:** profile creation always writes an explicit `config/local/node_id` (`<hostname>-<profile>`); wizard refuses a duplicate within the device's profile registry. `topology.json.brain_id` (§3.3) additionally guarantees a child can never be attached to the wrong brain even if node_ids collide across brains.
4. **App single-home assumption.** The app spec's supervisor, menu bar, notifications, and shims assume one home.
   **Fix (app-side, summarized in app spec §11):** `profiles.json` registry (`{name, home, accent}`); the supervisor runs one daemon per enabled profile concurrently; profile switcher in the window toolbar + menu bar section; per-profile accent tint on window chrome, queue, and notifications so the active brain is always visually unambiguous; per-profile shims (`brain-work`, `brain-personal`) alongside the `PKM_BRAIN_HOME`-aware generic `brain`.

### 5.3 What is shared on purpose

The provisioned Python runtime and downloaded model weights (read-only artifacts — sharing saves ~600MB per extra profile and has no data-leak surface). Nothing else: logs, backups, snapshots, telemetry, queues, tokens all stay per home. Each profile's sync topology is fully independent (different primaries, different epochs, different rosters).

### 5.4 Acceptance

Two profiles on one Mac, both daemons running: a Codex session under `~/work/**` appears in the work brain only (and in the personal brain's tick report as `routed_elsewhere`, not ingested); enabling `hyprnote` in the second profile is blocked until the claim moves; `pkm-brain-work` and `pkm-brain-personal` MCP round-trips hit different DBs (verified by distinct `write_agent_session` rows); node_ids distinct; a work-child and personal-child colocated on a third test home pair sync correctly against their respective primaries.

## 6. Phasing

| Phase | Contents | Depends on | Rides with |
|---|---|---|---|
| **T1 — Multi-child** | §2 (R2.1–R2.4) | app M0 daemon | app M3 (its three-home acceptance) |
| **T2 — Prerequisites** | §3 P1 relative paths, P2 snapshot replication, P3 topology.json | none (pure Python) | any time; **P2 is an off-machine backup win regardless — recommend early** |
| **T3 — Profiles** | §5 (claims, routing, MCP naming, app registry) | app M2 shell; connector registry M1 | post-M6 recommended |
| **T4 — Role mobility** | §4 (epochs, handover, disaster, demotion) | T1 + T2 | after the app is the stable daily driver |

Each phase: `ruff` + `pytest` (+ `xcodebuild` where Swift is touched) green; docs re-stamped; anything touching Peter's live machines gets a rollback path first (standing rule).

## 7. Hard rules

1. One writer per logical path; at most one primary per brain, at all times, including mid-transition.
2. `primary_epoch` is monotonic; a lower epoch is never obeyed; epochs never reset.
3. Promotion, demotion, and handover are human acts with typed confirmation and a printed data-loss statement. No automation may initiate them.
4. Snapshots are cold copies only; live SQLite/LanceDB never crosses machines.
5. Star topology only; children are leaves; brains never share state except through a human explicitly moving material.
6. A device-global capture source is ingested by exactly one profile per session — never two.
7. Derived state (mirrors, child DBs, indexes) is never authoritative; every transition rebuilds derived state rather than trusting it.
