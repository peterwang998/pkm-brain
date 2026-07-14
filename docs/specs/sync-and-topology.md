# Sync And Topology

**Status:** canonical living feature spec; Primary/Secondary knowledge transport is implemented, while operational replication, role mobility, and profiles are planned
**Last verified:** 2026-07-13 against the current source-sync implementation and the 2026-07-08 two-machine acceptance record
**Owns:** node roles, knowledge and operational writer ownership, sync transport, per-peer scheduling, coordinated recovery snapshots, role mobility, and multi-profile isolation

## Topology

A Brain is a star:

- exactly one primary;
- zero or more child/secondary nodes;
- children never sync directly with one another.

The primary owns canonical source material, Knowledge Curation policy, and all canonical Operational Chief-of-Staff state. A secondary captures local source artifacts into an outbox and maintains a rebuildable local knowledge mirror for retrieval.

Each Brain home has a stable, unique `node_id` and a role of `primary` or `secondary`. Multiple homes on one physical device require distinct node IDs.

## Authority And Paths

Primary-owned portable sources:

- `raw/`
- `wiki/`
- `memory/`
- `config/shared/`
- imported `inbox/external/<child>/` artifacts

Secondary-owned outbound sources:

- `outbox/<node_id>/agent_logs/`
- `outbox/<node_id>/documents/`
- `outbox/<node_id>/manifest.jsonl`

Never replicate as source:

- `db/brain.sqlite`, `db/ops.sqlite`, or any `*.sqlite*`
- `indexes/`
- `logs/`
- caches/temp files
- `config/local/`
- `config/sync.yaml`

SQLite, WAL/SHM, FTS, LanceDB, automation history, retrieval telemetry, and local scheduler state never travel through source sync. `brain.sqlite` contains local Knowledge Curation control state. `ops.sqlite` contains authoritative operational state on the primary and therefore requires coordinated recovery snapshots rather than live-file replication.

The two evidence flows meet only on the primary:

- portable source artifacts arrive through local capture or a validated child outbox, then enter deterministic ingest;
- Knowledge Curation may extract facts and project pages through its existing ledger and policy;
- Operational Chief-of-Staff detection may create source observations and reconcile operational items in `ops.sqlite`;
- neither flow transports private SQLite rows as source material or directly mutates the other's database.

## Configuration

Primary example:

```yaml
node_id: primary-laptop
role: primary
peers:
  - node_id: secondary-desktop
    role: secondary
    host: secondary.local
    user: local-user
    brain_home: ~/brain
    transport: ssh
    cadence_s: 1800
```

Secondary example:

```yaml
node_id: secondary-desktop
role: secondary
primary:
  node_id: primary-laptop
outbox:
  enabled: true
  path: ~/brain/outbox/secondary-desktop
```

Validation rejects unknown roles, duplicate/missing node IDs, malformed paths, and role-incompatible fields.

## Transport

The primary initiates both directions over pinned SSH/rsync when a child is reachable.

Pull:

```text
child outbox/<node_id>/
  -> primary staging
  -> manifest/hash/path validation
  -> primary inbox/external/<node_id>/
  -> primary ingest
```

Push:

```text
primary raw/wiki/memory/config/shared
  -> child mirror via delayed/partial-safe updates
  -> child sync rebuild-mirror-index
```

Rules:

- a failed/partial pull never mutates the live external inbox;
- path traversal, wrong origin, hash mismatch, or malformed manifests are rejected/quarantined;
- a failed primary ingest blocks push;
- push excludes all local/derived paths;
- a failed/partial push does not trigger remote rebuild;
- child rebuild uses its local embedding provider and index stamp;
- push does not include operational items, briefings, approvals, action plans, or execution receipts;
- `--if-reachable` offline behavior records a non-error skip.

## Capture And Origin

On a child, `brain automation secondary-tick` captures agent sessions and exports normalized artifacts plus manifest rows. Origin namespace prevents two machines' logical source paths from colliding.

On the primary, imported artifacts enter the same deterministic ingest pipeline as local sources. The latest-snapshot rule applies within the origin/logical-path identity.

Structured secondary `agent_sessions` database rows do not sync. Only source artifacts flow back in V1.

## Scheduling

The app daemon is the normal scheduler.

- primary: capture, nightly, and one independent `sync:<peer-node-id>` job per child;
- secondary: `secondary_tick` plus role-gated nightly stages;
- unreachable peer: successful skip with reason;
- one child's pause/failure does not change another child's configuration;
- mutation-capable Knowledge Curation stages skip on secondary by default; their current physical modules and tables may retain `cos_*` compatibility names;
- Operational Chief-of-Staff detection, reconciliation, feedback mutation, briefing generation, approval, and execution run only on the primary.

The daemon registry closes the old LaunchAgent single-peer-label limitation. Legacy LaunchAgent scheduler support remains for rollback/development. systemd and cron adapters are explicit unimplemented stubs.

## Security

- no public Brain endpoint or public inbound requirement;
- SSH BatchMode and pinned known-host entry;
- explicit remote user, host, Brain home, and node identity checks;
- no password/private-key contents stored in config;
- strict rsync include/exclude and destination validation;
- remote command is the packaged `brain` command under the configured home;
- connection tests verify role/node identity before transfer;
- external-action credentials and capability grants stay in primary-local configuration or credential storage and never cross source sync.

## Operational Writer And Execution Ownership

`ops.sqlite` has exactly one writer authority: the active primary. Every mutation-capable operational process verifies the configured node role before opening a write transaction. Once topology epochs are implemented, it must also verify the active `primary_epoch`; mismatch is fail-closed.

A secondary may contribute source evidence through its outbox but may not:

- detect directly into canonical operational state;
- reconcile, close, reschedule, or correct an item;
- approve or execute an external action;
- maintain a divergent local operational ledger that appears canonical.

Initial operational rollout is primary-local. A secondary without an approved read-only replica reports the operational surface unavailable. A future replica must be a separately specified read-only projection or an isolated restored copy derived from a coordinated recovery set; it may never be made by copying a live `ops.sqlite`, WAL, or SHM file, and the recovery artifact itself is never opened as the live mirror.

Guarded external execution is also primary-only. Sync reachability, source arrival, or a secondary request cannot constitute approval. The primary revalidates the exact payload, preconditions, capability, and reversibility class immediately before commit and records the resulting receipt in `ops.sqlite`.

The operational lifecycle and execution protocol are specified in [Chief-of-Staff Operations](chief-of-staff-operations.md). This spec owns where that authority may run, how it may be recovered, and what must never cross source sync.

## Status And Observability

`sync_runs` records peer, direction, timing, status, file/byte counts, primary ingest run, remote ingest status, and errors.

Status surfaces:

- reachability;
- last pull/push/success/failure;
- pending outbox when reachable;
- manifest/hash parity;
- mirror freshness;
- per-peer scheduler state;
- advisory path/content conflicts.

The native Ops peer matrix remains incomplete even though CLI/API state exists.

## Implemented Acceptance

The real primary/secondary pass completed on 2026-07-08 after app migration:

- primary app daemon ran `sync:Peters-Mac-mini` successfully;
- 37 files were pulled and 976 files pushed;
- primary ingest and remote ingest were successful;
- primary and child reported matching manifest hash;
- the child mirror was current with no warnings;
- child `secondary_tick` and app-managed runtime were active;
- three-home automated coverage verifies two independent children.

The operational checklist remains in [the sync acceptance runbook](../runbooks/sync-acceptance.md).

## Secondary Write Semantics

A secondary's UI/MCP reads its local knowledge mirror. Local Knowledge Curation UI/MCP writes modify only that secondary's local derived `brain.sqlite` and do not flow back in V1. Operational UI/MCP writes are disabled entirely.

Only outbox-captured source artifacts become eligible for primary Knowledge Curation or operational detection. The app must state this role boundary on write-capable surfaces so a local secondary knowledge approval is not mistaken for canonical state and an operational write is never offered locally.

## Role Mobility

Role mobility is planned and not implemented. It requires all prerequisites below before promotion/demotion controls can ship.

### Home-Relative Persistent Paths

Durable rows must not depend on one machine's absolute home path. A rebase/validation command must inventory and rewrite safe path fields while refusing ambiguous external references.

### Consistent DB Snapshot Replication

Source-only sync is not enough for lossless promotion because reviewed Knowledge Curation action/question/memory state, operational transitions and corrections, approvals, and execution receipts may not be reconstructable from Markdown.

A primary must produce one coordinated recovery set for `brain.sqlite` and `ops.sqlite` using SQLite backup/VACUUM semantics, never by copying either live database or its WAL/SHM files. The daemon establishes a short write barrier across both bounded contexts, records the shared source/ingest watermarks, snapshots each database, and releases the barrier only after both snapshots have a stable generation identity.

The recovery manifest records at minimum:

- Brain identity, primary node, and `primary_epoch` when available;
- one `backup_set_id`, creation time, and source-manifest/ingest watermarks;
- both database schema versions, filenames, byte sizes, and checksums;
- runtime compatibility and encryption/transport metadata;
- completion status that is written only after every member is durable.

The pair is encrypted/transported under the same trust boundary and stored as a recovery artifact on children. It is not opened concurrently as the child mirror database. Restore occurs into an isolated home, verifies both databases and the manifest, and rejects a mixed or incomplete generation by default. The restored home carries a local quarantine marker: the daemon and operational service fail closed until a future explicit activation workflow validates topology and clears that marker. Losing rebuildable indexes or briefing prose is acceptable; losing reviewed knowledge state, operational feedback, or execution audit is not.

### Shared Topology Record

A signed/checksummed topology record must name:

- brain identity;
- current primary node;
- monotonically increasing `primary_epoch`;
- child roster;
- last handover/snapshot;
- per-node role acknowledgment.

Every mutation-capable process must verify the active epoch. Epoch mismatch blocks mutation and prevents split brain.

### Planned Handover

Normal handover:

1. pause mutation jobs on old primary;
2. complete pull/ingest and make a final source + coordinated `brain.sqlite`/`ops.sqlite` recovery set;
3. verify candidate has the full state and compatible runtime/schema;
4. increment epoch and record new primary;
5. start new primary jobs;
6. demote old primary to child and verify round-trip.

Rollback remains available until the new primary completes acceptance.

### Disaster Promotion

Disaster promotion is explicit and warns about the recovery point. It requires the newest valid snapshot/source mirror, increments epoch, and fences any returning old primary before either side may mutate.

No last-writer-wins merge is permitted.

## Profiles

Multiple isolated brains on one Mac are planned and not implemented.

One profile means one Brain home, daemon, token, lock, queue, logs, backup set, config, and node identity. Runtime binaries and model weights may be shared read-only.

Required app registry:

```json
{"profiles": [{"name": "Work", "home": "/Users/me/brain-work", "accent": "blue"}]}
```

Isolation risks and required controls:

- device-global agent sources must be claimed by exactly one profile or routed by working-directory rules;
- Hyprnote-like sources without project metadata require exclusive claim;
- each profile gets a distinct MCP server registration;
- windows, notifications, and menu-bar actions visibly identify profile;
- backup/retention and sync node IDs are per profile;
- agents with multiple MCP servers remain a policy-level cross-brain leakage risk.

## Non-Goals

- direct live database replication;
- independent or mixed-generation replication of `brain.sqlite` and `ops.sqlite`;
- peer-to-peer child mesh;
- automatic conflict merging;
- automatic disaster election;
- public internet exposure;
- implicit cross-profile source sharing;
- secondary operational writers or executors.

## Acceptance

Current transport:

- real SSH/rsync pull, ingest, push, and child rebuild pass;
- no forbidden path crosses machines;
- two children remain independent;
- offline child records skip, not failure;
- secondary mutation-capable Knowledge Curation stages skip;
- mirror parity and outbox depth are observable.

Before operational rollout:

- every operational writer and guarded executor is primary-only;
- a secondary without an approved read-only projection reports the operational surface unavailable;
- source sync transfers no operational item, approval, plan, or receipt state.

Before role mobility:

- path inventory/rebase is complete;
- coordinated `brain.sqlite`/`ops.sqlite` snapshot and isolated restore drill passes;
- incomplete or mixed-generation recovery sets are rejected;
- topology epoch fences stale primaries;
- planned handover and disaster promotion each have rollback drills;
- returning old primary cannot mutate until demoted.

Before profiles:

- two daemons run concurrently without token/port/log collisions;
- global sources cannot be double claimed;
- UI and notifications identify the active profile;
- profile-specific MCP and sync acceptance pass.

Verification:

```bash
uv run brain sync doctor --home <primary-home>
uv run brain sync acceptance --home <primary-home> --peer <child> --json
uv run pytest tests/test_sync*.py tests/test_daemon.py -q
scripts/m3-migration-acceptance.sh
```
