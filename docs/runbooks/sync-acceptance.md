# Primary/Secondary Sync Acceptance Runbook

**Status:** current operational runbook
**Last verified:** 2026-07-10 against commit `e43e9c1e1287`; last real two-machine pass completed 2026-07-08
**Spec:** [Sync And Topology](../specs/sync-and-topology.md)

Run this before a sync/topology release or after changing SSH, paths, manifests, scheduler behavior, migration, or app runtimes. Unit/fake-transport tests do not prove real host-key, rsync, permission, sleep/wake, or time-skew behavior.

## Command Selection

Normal app-managed install:

```bash
brain --version
brain sync status --home ~/brain
```

Repository development:

```bash
uv run brain --version
uv run brain sync status --home ~/brain
```

Use one command prefix consistently in a run and record it below.

## Run Record

| Field | Value |
|---|---|
| Date | |
| Command/runtime path | |
| Primary hostname and `node_id` | |
| Child hostname/IP and `node_id` | |
| PKM Brain version/commit | |
| Python version | |
| rsync versions | |
| Primary app/daemon PID | |
| Child app/daemon PID | |

## Local Preflight

On the primary:

```bash
uv run ruff check .
uv run pytest -q
swift test --package-path app
brain doctor --home ~/brain
brain sync doctor --home ~/brain --json
brain sync acceptance --home ~/brain --peer <child-node-id> --json
```

Schema expectations:

- `schema_migrations` contains every version 1 through 20;
- migration 20 is `document_source_stats`;
- `sync_runs` contains peer/direction/timing/status/file/byte/ingest/remote-status/error fields;
- `PRAGMA integrity_check` returns `ok`;
- no migration is pending on either node.

Inspect without running concurrent long live-DB queries:

```bash
sqlite3 ~/brain/db/brain.sqlite \
  "SELECT version, applied_at FROM schema_migrations ORDER BY version;"
sqlite3 ~/brain/db/brain.sqlite "PRAGMA integrity_check;"
```

## Role And Security Checks

On both nodes:

1. `brain sync doctor --json` reports the expected role and node ID.
2. Brain home paths are the intended distinct local paths.
3. App daemon is healthy and uses the expected runtime version.
4. No legacy pkm-brain LaunchAgent is active after app migration.

From primary to child:

```bash
brain sync test-connection <child-node-id> --home ~/brain
```

Record:

| Check | Result |
|---|---|
| SSH BatchMode | |
| pinned host fingerprint | |
| remote role/node identity | |
| remote Brain home | |
| remote command version | |
| outbox read/write/delete probe | |
| local/remote rsync probes | |

Never accept a changed host key automatically during acceptance.

## Source Round Trip

Use a unique phrase that contains no secrets and will be easy to remove later.

1. On the child, create/capture one agent session containing the phrase.
2. Run `brain automation secondary-tick --home <child-home>` or the app's run-now equivalent.
3. Verify the child outbox artifact and `manifest.jsonl` row.
4. On the primary, run:

```bash
brain sync run <child-node-id> --home ~/brain
```

5. Verify pull landed under `inbox/external/<child-node-id>/` through staging.
6. Verify primary ingest succeeded and retrieval finds the phrase.
7. Verify push mirrored `raw/`, `wiki/`, `memory/`, and `config/shared/`.
8. Verify no `db/`, `indexes/`, `logs/`, `config/local/`, `config/sync.yaml`, or outbox content was overwritten.
9. Verify remote rebuild succeeded and child retrieval finds the phrase.
10. Verify manifest/hash parity and `mirror_current=true`.

Automated report:

```bash
brain sync acceptance \
  --home ~/brain \
  --peer <child-node-id> \
  --run-sync \
  --retrieval-phrase "<unique phrase>" \
  --json
```

`complete: true` means the automated checks passed. It does not replace the observed child-side retrieval and app/scheduler checks.

## Scheduler Checks

Primary:

- one `sync:<child-node-id>` job exists per configured child;
- each job reports its own cadence, due time, status, and no-op reason;
- pausing one child does not affect another;
- offline `--if-reachable` records a skip, not failure.

Child:

- `secondary_tick` exists and advances capture/outbox state;
- mutation-capable CoS stages report role-gated skip;
- no child-initiated primary sync job exists.

For two configured children, run the three-home acceptance harness and verify both origin namespaces/mirrors independently.

## Failure Checks

Run non-destructive cases:

| Scenario | Expected | Result |
|---|---|---|
| child offline with `--if-reachable` | skipped, no push | |
| wrong remote role/node ID | fail before rsync | |
| bad/changed host key | hard fail | |
| malformed/path-traversal manifest | reject/quarantine, no live inbox mutation | |
| pull rsync partial/failure | staging preserved, live inbox untouched | |
| primary ingest failure | push blocked | |
| push partial/failure | remote rebuild blocked, mirror degraded | |
| remote runtime/version mismatch | actionable failure | |

## Secondary Write Warning

Confirm the app explains that Queue/MCP writes on a child modify only the child's local derived DB and do not sync to the primary in V1. Only outbox source captures flow back.

## Last Completed Real Run

Recorded 2026-07-08 after app migration:

- primary `sync:Peters-Mac-mini` status `ok`;
- 37 files pulled;
- 976 files pushed;
- primary ingest and remote ingest `ok`;
- matching manifest hash `659ae5b786e220104e3175fca05b6247be7a9633834e8c4a8a3772ff4e8d7f46`;
- `mirror_current=true`;
- no sync warnings;
- child app runtime and `secondary_tick` healthy.

This record proves that release only. Re-run after relevant transport/runtime changes.

## Sign-Off

Acceptance is complete only when:

- Ruff, Python, Swift, and release app gates pass;
- schema 1-20 and integrity checks pass on both nodes;
- role/security, source round trip, scheduler, and required failure checks pass;
- mirror parity and retrieval succeed on both nodes;
- any failure is fixed or recorded as a release blocker with owner and rollback.
