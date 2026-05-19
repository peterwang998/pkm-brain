# Primary/Secondary Sync Acceptance Runbook

Use this runbook before tagging V1. Unit and fake-transport tests do not cover
real SSH host-key behavior, real rsync behavior, filesystem permissions, or
time skew between machines.

## Preflight

Record the machines and versions used for the acceptance run:

| Field | Value |
| --- | --- |
| Date | |
| Primary hostname | |
| Primary node_id | |
| Secondary hostname/IP | |
| Secondary node_id | |
| PKM Brain commit | |
| Python version | |
| rsync version on Primary | |
| rsync version on Secondary | |

Run local validation on the Primary:

```bash
uv run pytest -q
uv run ruff check .
sqlite3 ~/brain/db/brain.sqlite "SELECT version, applied_at FROM schema_migrations ORDER BY version;"
sqlite3 ~/brain/db/brain.sqlite "PRAGMA table_info(sync_runs);"
uv run brain sync acceptance --home ~/brain --peer <secondary-node-id> --json
```

Expected schema state:

- `schema_migrations` includes versions `1` and `2`.
- `sync_runs` has 13 columns: `id`, `peer_node_id`, `direction`,
  `started_at`, `finished_at`, `status`, `files_pulled`, `files_pushed`,
  `bytes_pulled`, `bytes_pushed`, `primary_ingest_run_id`,
  `remote_ingest_status`, and `errors`.

## Setup

On the Primary:

```bash
uv run brain setup
uv run brain sync doctor --home ~/brain --json
```

On the Secondary:

```bash
uv run brain setup
uv run brain sync doctor --home ~/brain --json
```

Confirm SSH from Primary to Secondary works without a password prompt for the
configured identity, then validate Brain-level reachability:

```bash
uv run brain sync test-connection <secondary-node-id> --home ~/brain
```

Record the result:

| Check | Result |
| --- | --- |
| Primary sync doctor ready | |
| Secondary sync doctor ready | |
| SSH BatchMode probe | |
| Remote `brain sync doctor` probe | |
| Outbox read/write/delete probe | |
| Local and remote rsync probes | |
| Host-key fingerprint pinned | |

## Acceptance Flow

Complete each step and paste the key command output or observation into the
Result column.

| Step | Command or observation | Result |
| --- | --- | --- |
| 1 | Configure laptop as Primary. | |
| 2 | Configure LAN-only secondary machine as Secondary. | |
| 3 | `uv run brain sync test-connection <secondary-node-id> --home ~/brain` | |
| 4 | Start a Codex run on the Secondary, including a Codex Mobile-triggered run. | |
| 5 | `uv run brain automation secondary-tick --home ~/brain` on Secondary. | |
| 6 | `uv run brain sync run <secondary-node-id> --home ~/brain` on Primary. | |
| 7 | Confirm Primary has Secondary files under `~/brain/inbox/external/<secondary-node-id>/`. | |
| 8 | Confirm Primary ingest result has no errors. | |
| 9 | `uv run brain retrieve-context --home ~/brain --task "<unique Secondary session phrase>"` finds the Secondary session. | |
| 10 | Confirm push mirrored canonical `raw/`, `wiki/`, `memory/`, and `config/shared/` content to Secondary. | |
| 11 | Confirm Secondary retrieval sees the pushed source after rebuild/remote ingest. | |

The automated portion can run the connection test, real sync, and Primary
retrieval verification in one report:

```bash
uv run brain sync acceptance \
  --home ~/brain \
  --peer <secondary-node-id> \
  --run-sync \
  --retrieval-phrase "<unique Secondary session phrase>" \
  --json
```

`complete: true` means the automated checks passed. The table above should
still be filled in with the observed real-machine setup and Secondary-side
retrieval result.

## Failure Checks

Run at least the non-destructive failure checks:

| Scenario | Expected | Result |
| --- | --- | --- |
| Secondary offline with `--if-reachable` | `sync_runs.status = skipped`, no push. | |
| Wrong remote role or node ID | `test-connection` fails before rsync. | |
| Bad or changed host key | Connection hard-fails; do not auto-trust. | |

## Sign-Off

V1 acceptance is complete only when:

- Full tests and Ruff pass on the acceptance commit.
- The real upgraded Primary DB shows migrations `1` and `2`.
- The 11-step acceptance flow passes on real machines.
- Any observed failures are either fixed or logged as explicit post-V1 issues.
