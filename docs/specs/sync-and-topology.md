# Sync And Topology

**Status:** canonical living feature spec; Primary/Secondary transport is implemented, role mobility and profiles are planned
**Last verified:** 2026-07-11 against public release `0.1.1` code snapshot `b3ba211` and the 2026-07-08 two-machine acceptance record
**Owns:** node roles, sync ownership/transport, per-peer scheduling, role mobility, snapshot replication, and multi-profile isolation

## Topology

A Brain is a star:

- exactly one primary;
- zero or more child/secondary nodes;
- children never sync directly with one another.

The primary owns canonical source material and policy. A secondary captures local source artifacts into an outbox and maintains a rebuildable local mirror for retrieval.

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

- `db/` or `*.sqlite*`
- `indexes/`
- `logs/`
- caches/temp files
- `config/local/`
- `config/sync.yaml`

SQLite, WAL/SHM, FTS, LanceDB, automation history, retrieval telemetry, and local scheduler state remain machine-local derived state.

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
- mutation-capable CoS stages skip on secondary by default.

The daemon registry closes the old LaunchAgent single-peer-label limitation. Legacy LaunchAgent scheduler support remains for rollback/development. systemd and cron adapters are explicit unimplemented stubs.

## Security

- no public Brain endpoint or public inbound requirement;
- SSH BatchMode and pinned known-host entry;
- explicit remote user, host, Brain home, and node identity checks;
- no password/private-key contents stored in config;
- strict rsync include/exclude and destination validation;
- remote command is the packaged `brain` command under the configured home;
- connection tests verify role/node identity before transfer.

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

A secondary's UI/MCP reads its local mirror. Local UI/MCP writes modify only that secondary's local derived database and do not flow back in V1.

Only outbox-captured source artifacts become primary knowledge. The app must state this role boundary on write-capable surfaces so a local secondary approval is not mistaken for canonical state.

## Role Mobility

Role mobility is planned and not implemented. It requires all prerequisites below before promotion/demotion controls can ship.

### Home-Relative Persistent Paths

Durable rows must not depend on one machine's absolute home path. A rebase/validation command must inventory and rewrite safe path fields while refusing ambiguous external references.

### Consistent DB Snapshot Replication

Source-only sync is not enough for lossless promotion because reviewed action/question/memory state and telemetry may not be reconstructable from Markdown.

A primary must produce a consistent SQLite snapshot using SQLite backup/VACUUM semantics, never by copying a live database/WAL. The snapshot is checksummed, versioned, encrypted/transported under the same trust boundary, and stored as a recovery artifact on children. It is not opened concurrently as the child mirror DB.

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
2. complete pull/ingest and make a final source + DB snapshot;
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
- peer-to-peer child mesh;
- automatic conflict merging;
- automatic disaster election;
- public internet exposure;
- implicit cross-profile source sharing.

## Acceptance

Current transport:

- real SSH/rsync pull, ingest, push, and child rebuild pass;
- no forbidden path crosses machines;
- two children remain independent;
- offline child records skip, not failure;
- secondary mutation-capable CoS stages skip;
- mirror parity and outbox depth are observable.

Before role mobility:

- path inventory/rebase is complete;
- consistent snapshot and restore drill passes;
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
