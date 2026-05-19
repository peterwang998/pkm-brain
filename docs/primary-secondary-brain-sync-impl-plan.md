# Primary / Secondary Brain Sync — Implementation Plan

Plan version: 0.2
Spec source: [`primary-secondary-brain-sync-spec.md`](./primary-secondary-brain-sync-spec.md) (v0.1, 2026-05-18)
Last updated: 2026-05-18

This plan is the build-out path for the spec. It is structured for a coding agent (codex) to execute milestone-by-milestone. Each milestone defines its scope, implementation steps, tests to author, and **validation checks** — concrete commands that must pass before the milestone is considered complete.

---

## 1. Repo Grounding

Current layout to build against:

```text
pkm-brain/
  src/pkm_brain/
    cli.py              # all `brain ...` commands
    service.py          # service-layer functions; ingest lives here
    capture.py          # agent log capture
    db.py               # sqlite schema + helpers
    automation.py       # nightly job orchestrator
    paths.py            # brain_home layout
    audit.py, embeddings.py, indexes.py, llm.py, mcp_server.py,
    memory_proposals.py, wiki.py, wiki_proposals.py, chunking.py
  tests/                # currently: test_capture.py, test_core.py
  docs/
    personal-knowledge-management-spec-v0.1.md
    primary-secondary-brain-sync-spec.md
    primary-secondary-brain-sync-impl-plan.md   # this file
```

There is no `sync_config.py`, no `brain sync` group, no `brain setup`, no scheduler abstraction, and no Web UI. Several spec-listed prerequisites must be added before the transport layer can land safely (origin-aware identity, atomic schema migration, memory export). M1 absorbs those prerequisites.

---

## 2. Milestone Overview

| ID | Title                                                  | Ships independently? |
|----|--------------------------------------------------------|----------------------|
| M1 | Foundations: service JSON, origin identity, migrations, memory export, sync config, doctor | yes |
| M2 | Role init, peer registration, SSH validation with pinned host keys | yes |
| M3 | Rsync transport: outbox export, staged pull, atomic push, secondary-tick | yes |
| M4 | Observability (`sync_runs`), scheduler adapter abstraction | yes |
| M5 | Installer wizard (`brain setup`) and local Web UI control plane | yes |

Each milestone is sequenced. M2 depends on M1's `sync_config`. M3 depends on M1's origin identity + memory export. M4 depends on M1's migration helper. M5 depends on M1–M4.

---

## 3. Definition of Done (applies to every milestone)

A milestone is complete only when **all** the following are true:

1. `uv run pytest -q` passes with the new test files included.
2. `uv run ruff check .` passes.
3. Every "Validation check" listed for the milestone returns the expected result.
4. Public CLI commands print the documented `--help` and accept the documented flags.
5. No regression in existing tests (`test_capture.py`, `test_core.py`).
6. Any new SQLite schema change is reachable via the migration helper (M1+).
7. No file under `db/`, `indexes/`, `logs/`, or any `*.sqlite*` is written into a sync-eligible directory or referenced by sync code paths.

---

## 4. Milestone M1 — Foundations

### 4.1 Goals

- Make the service layer JSON-safe so both CLI `--json` and the future Web UI consume the same surface.
- Land **origin-aware document identity** before any sync code dedupes wrong.
- Land a **schema migration helper** before any new tables ship.
- Land **memory export/import** so `memory/` is plain files that can be mirrored.
- Land **sync config parsing** and `brain sync doctor` so installs can self-check.
- Split `config/` into `config/shared/` and `config/local/`.

### 4.2 Implementation steps

**M1.1 — Schema migration helper**

- Add `src/pkm_brain/migrations.py`:
  - `schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)` table.
  - Registry of `(version: int, name: str, fn: Callable[[sqlite3.Connection], None])`.
  - `run_migrations(conn)` applies all unapplied versions in a single transaction per migration.
- Call `run_migrations(conn)` from the existing connection bootstrap in `db.py` after `CREATE TABLE IF NOT EXISTS` runs.
- Existing `ensure_column` calls keep their behavior; new schema work goes through migrations.

**M1.2 — Origin-aware document identity**

Existing dedupe path (`service.py` ingest) currently keys on `content_hash` and updates `source_path` on collision; latest-snapshot retention keys on `source_path` alone. Both collapse Primary + Secondary records that must coexist.

- Migration `001_add_origin_identity`:
  - `ALTER TABLE documents ADD COLUMN origin_node_id TEXT`
  - `ALTER TABLE documents ADD COLUMN logical_source_key TEXT`
  - `CREATE INDEX idx_documents_origin_logical ON documents(origin_node_id, logical_source_key)`
  - Backfill: `origin_node_id = '<local>'` for existing rows; `logical_source_key = source_path`.
- Local node id resolution: `paths.local_node_id(home)` — reads `config/local/node_id` if present, else `socket.gethostname()`. Used as default for any locally captured doc.
- Update ingest dedupe in `service.py`:
  - Lookup key changes from `content_hash` alone to `(origin_node_id, logical_source_key)`.
  - On collision: update content_hash, raw_path, timestamps; do not collapse different origins.
- Update latest-snapshot retention in `service.py`:
  - Key changes from `source_path` to `(origin_node_id, source_path)`.
- All existing local capture paths must populate `origin_node_id = local_node_id(home)` and `logical_source_key = source_path` (preserves current behavior for solo machines).

**M1.3 — Memory file export/import**

- On `brain memory approve <id>`: write `memory/<scope>/<memory_id>.md` with YAML frontmatter (`memory_type`, `scope`, `confidence`, `source_ids`, `reviewed_at`, `reviewed_by`, `status`) and the memory body. Deletion on `reject`/`archive` is by status update + file removal.
- Add `brain memory export-all` — idempotent rewrite of all `active` memories to disk.
- Add `brain memory import --from <dir>` — upsert by `memory_id`, refuse import if a memory's source_ids reference unknown documents (configurable with `--allow-missing-sources`).
- SQLite remains canonical; `memory/` is a derived but **sync-eligible** export.

**M1.4 — Config split**

- Move single-file `config/config.yaml` writer to `config/local/config.yaml`. Backwards-compat read shim: if `config/local/config.yaml` is missing but `config/config.yaml` exists, read the old path and emit a deprecation warning once per process.
- Create `config/shared/` (empty) on `brain init`.
- `brain init` creates: `raw/ wiki/ memory/ inbox/ db/ indexes/ logs/ config/local/ config/shared/`.

**M1.5 — Service layer JSON surface**

- In `service.py`, expose `as_dict()` (or equivalent) for: doctor result, index status, ingest summary, capture status, memory list/inspect.
- All structures must be `json.dumps`-able without a custom encoder.
- Update `cli.py` doctor / status commands to accept `--json` and call the same service functions.

**M1.6 — Sync config + doctor**

- Add `src/pkm_brain/sync_config.py`:
  - Typed `SyncConfig`, `PrimaryConfig`, `SecondaryConfig`, `PeerConfig` (dataclasses with explicit validators).
  - `load_sync_config(home) -> SyncConfig` reading `config/sync.yaml`.
  - Validators: unknown role → `ValueError`; missing `node_id` → `ValueError`; duplicate peer `node_id` → `ValueError`; any `peers[].mirror_paths` value matching `db/|indexes/|logs/|*.sqlite*` → `ValueError`; secondary `outbox.path` must contain the secondary `node_id` → `ValueError`.
- Add `brain sync doctor` (and `--json`) covering the §11 checklist: `sync.yaml` exists, `node_id` set, `role` valid, role-specific fields present, required dirs creatable, `brain_home` matches, local-only paths are not mirrored.

### 4.3 Tests to author

| File                          | Cases |
|-------------------------------|-------|
| `tests/test_migrations.py`    | Fresh DB applies all migrations; rerun is a no-op; `schema_migrations` records each version; partial failure rolls back that migration. |
| `tests/test_origin_identity.py` | Local ingest stamps `origin_node_id=<local>`; two ingests with same `source_path` but different `origin_node_id` produce two rows; latest-snapshot retention preserves both; re-ingest of same `(origin, source_path)` updates one row. |
| `tests/test_memory_export.py` | `approve` writes `memory/<scope>/<id>.md`; `export-all` is idempotent (byte-identical second run); `import --from` upserts; missing-source memory refused without `--allow-missing-sources`. |
| `tests/test_config_split.py`  | Fresh `brain init` creates `config/local/` and `config/shared/`; legacy `config/config.yaml` is read with deprecation warning; new writes target `config/local/`. |
| `tests/test_sync_config.py`   | Valid primary/secondary configs round-trip; missing `role` raises; duplicate peer `node_id` raises; mirror path covering forbidden dirs raises; secondary outbox without `node_id` raises. |
| `tests/test_sync_doctor.py`   | Doctor passes on init'd primary and secondary workspaces; flags missing fields; `--json` output has stable schema. |
| `tests/test_service_json.py`  | Every service `as_dict()` is `json.dumps`-able. |

### 4.4 Validation checks (codex runs these)

```bash
# 1. Tests green
uv run pytest tests/test_migrations.py tests/test_origin_identity.py \
  tests/test_memory_export.py tests/test_config_split.py \
  tests/test_sync_config.py tests/test_sync_doctor.py \
  tests/test_service_json.py -q

# 2. Schema present
sqlite3 ~/brain/db/brain.sqlite \
  "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_migrations';" \
  | grep -q schema_migrations

sqlite3 ~/brain/db/brain.sqlite \
  "PRAGMA table_info(documents);" | grep -q origin_node_id

sqlite3 ~/brain/db/brain.sqlite \
  "PRAGMA table_info(documents);" | grep -q logical_source_key

# 3. CLI surfaces exist
uv run brain sync doctor --help | grep -q -- --json
uv run brain memory export-all --help
uv run brain memory import --help

# 4. Config split
test -d ~/brain/config/local && test -d ~/brain/config/shared

# 5. Doctor JSON shape
uv run brain sync doctor --json | python -c \
  "import json,sys; d=json.load(sys.stdin); \
   assert 'role' in d and 'node_id' in d and 'checks' in d, d"

# 6. Memory export idempotency
uv run brain memory export-all
hash1=$(find ~/brain/memory -type f -name '*.md' -exec shasum {} + | shasum | awk '{print $1}')
uv run brain memory export-all
hash2=$(find ~/brain/memory -type f -name '*.md' -exec shasum {} + | shasum | awk '{print $1}')
test "$hash1" = "$hash2"
```

---

## 5. Milestone M2 — Role init, peer registration, SSH validation

### 5.1 Goals

- Operator can configure Primary or Secondary role and add peers.
- All SSH probes use a **pinned host key**; no opportunistic trust.
- No secrets stored in `sync.yaml` (paths + fingerprints only).

### 5.2 Implementation steps

**M2.1 — `brain sync init-primary` / `init-secondary`**

- Interactive by default; `--yes` plus per-field flags for scripting. Prompts per spec §10.2.
- Writes `config/sync.yaml`. Refuses to overwrite an existing config unless `--force`.
- Creates required directories: Primary → `inbox/external/`; Secondary → `outbox/<node_id>/`.

**M2.2 — `brain sync add-peer`**

- Interactive prompts: `node_id`, host, user, remote brain_home, identity path, `--allow-first-host-key` toggle, "test connection now?".
- Validates that local role is `primary`.
- Refuses duplicate `node_id`.

**M2.3 — Host-key pinning (concrete flow)**

- New helper `src/pkm_brain/sync_ssh.py`:
  - `fetch_host_keys(host) -> list[HostKeyCandidate]` via `ssh-keyscan -t ed25519,rsa -T 5 <host>`.
  - `fingerprint(key) -> str` via `ssh-keygen -lf -`.
  - `pinned_known_hosts_path(home) = home / 'config' / 'local' / 'known_hosts'` (peer-scoped, never `~/.ssh/known_hosts`).
- On `--allow-first-host-key`:
  1. Fetch candidates.
  2. Print fingerprints, ask user to confirm out-of-band.
  3. Append accepted line to pinned `known_hosts`.
  4. Write `peers[].host_key_fingerprint` into `sync.yaml`.
- All sync SSH invocations use:

  ```text
  -o UserKnownHostsFile=<pinned known_hosts>
  -o StrictHostKeyChecking=yes
  -o BatchMode=yes
  -o ConnectTimeout=5
  ```

- Mismatched fingerprint on any subsequent run → hard fail with pinned vs observed in error.

**M2.4 — `brain sync test-connection <peer>`**

Sequence per spec §11:

1. SSH liveness (`true`).
2. Remote `brain` resolvable (`command -v brain` or configured path).
3. Remote `brain sync doctor --json --home <remote_home>` returns `role=secondary`, matching `node_id`, matching `brain_home`.
4. Remote outbox probe: write/read/delete `_probe-<uuid>` under `outbox/<node_id>/`.
5. `rsync --version` local and remote.

Output: human and `--json` exactly matching spec §12.

### 5.3 Tests to author

| File | Cases |
|------|-------|
| `tests/test_sync_ssh.py` | `fingerprint()` returns SHA256 form; pinned `known_hosts` written under `config/local/`; SSH argv contains `StrictHostKeyChecking=yes` and `BatchMode=yes`; never references `~/.ssh/known_hosts`. |
| `tests/test_sync_init.py` | `init-primary --yes` writes valid config; rerun without `--force` refuses; `init-secondary` requires `--primary-node-id`. |
| `tests/test_sync_add_peer.py` | Adds peer; duplicate `node_id` refused; refuses when local role is secondary. |
| `tests/test_sync_test_connection.py` | Uses an in-process fake SSH transport (`tests/fake_transport.py`); passes against compliant fake; fails on role mismatch, node mismatch, missing outbox, missing rsync. JSON matches §12. |

A `tests/fake_transport.py` `Transport` protocol is introduced here:

```python
class Transport(Protocol):
    def run(self, host: str, argv: list[str]) -> SubprocessResult: ...
    def rsync(self, args: list[str]) -> SubprocessResult: ...
```

Production transport shells out; tests use an in-process fake that executes against a second tempdir.

### 5.4 Validation checks

```bash
# 1. Tests
uv run pytest tests/test_sync_ssh.py tests/test_sync_init.py \
  tests/test_sync_add_peer.py tests/test_sync_test_connection.py -q

# 2. Init writes config
rm -rf /tmp/brain-m2-primary && uv run brain --home /tmp/brain-m2-primary init
uv run brain --home /tmp/brain-m2-primary sync init-primary \
  --node-id primary-test --yes
test -f /tmp/brain-m2-primary/config/sync.yaml
grep -q "role: primary" /tmp/brain-m2-primary/config/sync.yaml

# 3. Re-init refused without --force
! uv run brain --home /tmp/brain-m2-primary sync init-primary \
    --node-id primary-test --yes 2>/dev/null

# 4. Pinned known_hosts location (after a fake add-peer in tests)
uv run brain --home /tmp/brain-m2-primary sync add-peer --help \
  | grep -q -- --allow-first-host-key

# 5. test-connection JSON shape
uv run brain sync test-connection --help | grep -q -- --json
```

---

## 6. Milestone M3 — Rsync transport

### 6.1 Goals

- Outbox export from Secondary captures.
- Pull is **staged** so a partial rsync cannot damage the live external inbox.
- Push is **atomic per file** so a partial rsync cannot leave a half-overwritten mirror.
- Secondary self-ingests its own captures via `secondary-tick`.

### 6.2 Implementation steps

**M3.1 — Rsync command builder**

- `src/pkm_brain/sync_rsync.py` exposes pure builders returning `list[str]`:
  - `build_pull(peer, run_id) -> list[str]`
  - `build_push(peer, source_subdir) -> list[str]`
- Mandatory pull excludes: none (the outbox is bounded by construction).
- Mandatory push excludes (all source subdirs): `db/`, `indexes/`, `logs/`, `*.sqlite`, `*.sqlite-wal`, `*.sqlite-shm`, `.DS_Store`, `cache/`, `tmp/`.
- Push must never target: `config/sync.yaml`, `config/local/`, `outbox/`.
- Push uses `--delay-updates --partial-dir=.rsync-partial` so files appear atomically on the remote.

**M3.2 — Outbox export with idempotent manifest**

- Extend `capture.py` with `--export-outbox`:
  - Hard-link (fallback: copy) inbox capture into `outbox/<node_id>/agent_logs/<agent>/`.
  - Rewrite `outbox/<node_id>/manifest.jsonl` atomically (`manifest.jsonl.tmp` → `os.rename`).
  - Manifest invariant: **exactly one row per `relative_path`**, keyed by content_hash. Identical content is a no-op. Modified content updates that row in place.
- Manifest row shape per spec §10.4.

**M3.3 — Pull with staging**

- `brain sync pull <peer>` flow:
  1. Generate `run_id` (uuid).
  2. `rsync` into `inbox/external/<peer_node>/_staging/<run_id>/`.
  3. Verify each file's sha256 against `manifest.jsonl`. Mismatches move to `_staging/<run_id>/_rejected/<rel>` with sibling `.error.json` (`{"expected_hash":..., "observed_hash":..., "reason":"hash_mismatch"}`).
  4. Promote: per file, `os.rename` from staging into the live `inbox/external/<peer_node>/<rel>` path (atomic on same FS).
  5. Delete empty staging dirs; preserve `_rejected/` until explicitly cleaned.
  6. Trigger `brain ingest` for the live external inbox; documents stamped `origin_node_id=<peer_node>`, `logical_source_key=<rel>`.
- The live `inbox/external/<peer_node>/` is **never** an rsync target — `--delete` can't damage prior state.

**M3.4 — Push (atomic per file)**

- `brain sync push <peer>` runs separate rsyncs for `raw/`, `wiki/`, `memory/`, `config/shared/`. Each uses `--delay-updates`.
- Per-document ingest quarantine on the **remote** side keeps damaged files out of canonical state (M3.5 plus existing ingest semantics).

**M3.5 — Per-document ingest quarantine**

- When `brain ingest` encounters a per-file failure during processing of `inbox/external/<peer_node>/`, move the offending file to `inbox/external/<peer_node>/_quarantine/<rel>` and write `<rel>.error.json` with traceback summary. Continue processing siblings.
- Add `brain ingest --retry-quarantine` to re-attempt all quarantined files.

**M3.6 — `secondary-tick` and `--also-ingest`**

- `brain capture agents --export-outbox --also-ingest` — runs capture, exports outbox, then runs local ingest so the Secondary's own captures are immediately searchable locally.
- New higher-level command `brain automation secondary-tick` — equivalent to the above plus refreshes index status. This is the command the Secondary scheduled job will call (M4).

**M3.7 — `brain sync run <peer>` (orchestrator)**

Order: pull → primary ingest → push → optional remote ingest. Failure semantics per §9 below.

### 6.3 Tests to author

| File | Cases |
|------|-------|
| `tests/test_sync_rsync_builder.py` | Pull argv has expected source/dest and `-az --delete`. Push argv per subdir has expected excludes, `--delay-updates`, `--partial-dir`. Push never targets forbidden paths. |
| `tests/test_capture_outbox.py` | `--export-outbox` writes manifest row with correct hash. Identical re-export is a no-op (file mtime + manifest byte-identical). Modified content updates the same row (no duplicate `relative_path`). |
| `tests/test_sync_pull_staging.py` | Successful pull promotes files atomically into live inbox; staging is empty after. Pull with one hash-mismatched file leaves only that file in `_rejected/` with `.error.json`; live inbox unchanged for that path; siblings promoted. Simulated rsync failure mid-pull leaves staging present and live inbox untouched. |
| `tests/test_sync_pull_ingest.py` | Pulled documents are stamped `origin_node_id=<peer_node>`. Primary and Secondary capturing same Codex `session_id` produce two distinct documents post-pull. |
| `tests/test_sync_push.py` | Push per subdir copies into remote tempdir; `db/`, `indexes/`, `logs/`, `*.sqlite*` never appear; `config/sync.yaml`, `config/local/`, `outbox/` never overwritten. |
| `tests/test_ingest_quarantine.py` | A document causing ingest to raise is moved to `_quarantine/` with `.error.json`; ingest run completes; `--retry-quarantine` re-attempts. |
| `tests/test_secondary_tick.py` | `secondary-tick` runs capture, writes outbox manifest, runs local ingest, and the captured doc is retrievable locally. |
| `tests/test_sync_run.py` | Orchestrator runs pull→ingest→push→remote-ingest in order; aborts push if ingest fails; records nothing in `sync_runs` yet (M4 wires that up). |

### 6.4 Validation checks

```bash
# 1. Tests
uv run pytest tests/test_sync_rsync_builder.py tests/test_capture_outbox.py \
  tests/test_sync_pull_staging.py tests/test_sync_pull_ingest.py \
  tests/test_sync_push.py tests/test_ingest_quarantine.py \
  tests/test_secondary_tick.py tests/test_sync_run.py -q

# 2. Rsync builder excludes — programmatic check
uv run python -c "
from pkm_brain.sync_rsync import build_push
from pkm_brain.sync_config import PeerConfig
peer = PeerConfig(node_id='x', host='h', user='u', brain_home='/r')
for sub in ['raw/', 'wiki/', 'memory/', 'config/shared/']:
    argv = build_push(peer, sub)
    for needle in ['db/', 'indexes/', 'logs/', '*.sqlite', '--delay-updates']:
        assert any(needle in a for a in argv), (sub, needle)
print('ok')
"

# 3. Live inbox never an rsync target — grep the source for the contract
! grep -RInE "inbox/external/.+(?<!staging)/?\s*\$\s*--" src/pkm_brain/sync_rsync.py || \
  echo "Inspect manually: pull must target _staging only"

# 4. Manifest idempotency
uv run brain capture agents --export-outbox >/dev/null
hash1=$(shasum ~/brain/outbox/*/manifest.jsonl | awk '{print $1}')
uv run brain capture agents --export-outbox >/dev/null
hash2=$(shasum ~/brain/outbox/*/manifest.jsonl | awk '{print $1}')
test "$hash1" = "$hash2"

# 5. Secondary-tick exists and runs end-to-end
uv run brain automation secondary-tick --help
```

---

## 7. Milestone M4 — Observability + scheduler abstraction

### 7.1 Goals

- Every sync run produces an auditable `sync_runs` row.
- `brain sync status` answers "are we healthy and is the mirror current?"
- Scheduler is platform-pluggable; macOS LaunchAgents are the first adapter.

### 7.2 Implementation steps

**M4.1 — `sync_runs` table**

- Migration `002_create_sync_runs` per spec §15 schema. All writes go through a `record_sync_run()` helper in `service.py` so failure paths can't forget to record.
- Statuses: `ok`, `ok_with_remote_ingest_failure`, `failed`, `skipped`.

**M4.2 — `brain sync status` and `brain sync conflicts`**

- `status`: per peer, last successful pull/push timestamps, last failed run summary, pending outbox count if reachable, manifest-hash parity between Primary canonical and Secondary mirror.
- `conflicts`: advisory list of logical source paths observed under two `origin_node_id` namespaces.

**M4.3 — Scheduler adapter**

- `src/pkm_brain/scheduler/__init__.py` defines:

  ```python
  class Scheduler(Protocol):
      def install(self, job: ScheduledJob) -> None: ...
      def uninstall(self, label: str) -> None: ...
      def status(self, label: str | None = None) -> list[JobStatus]: ...
  ```

- `scheduler/launchd.py` — current LaunchAgent code refactored behind the protocol. Adds support for the new labels `com.pkm-brain.sync-primary` and `com.pkm-brain.capture-secondary`.
- `scheduler/systemd.py`, `scheduler/cron.py` — V1 stubs raising `NotImplementedError("Linux scheduler not yet implemented; use on-demand commands or launchd on macOS")`. Real impls deferred.
- New CLI:
  - `brain scheduler install-sync --peer <node> --interval 1800`
  - `brain scheduler install-secondary-capture --interval 600` (invokes `brain automation secondary-tick`)
  - `brain scheduler status`
  - `brain scheduler uninstall-sync --peer <node>`
  - `brain scheduler uninstall-secondary-capture`
- Existing `brain launch-agent ...` commands keep working as lower-level adapters.

### 7.3 Tests to author

| File | Cases |
|------|-------|
| `tests/test_sync_runs_schema.py` | Migration creates table with all spec §15 columns. |
| `tests/test_sync_run_recording.py` | Successful run writes one row, `status=ok`. Push-failed run writes `status=failed` with `errors` populated. `--if-reachable` skip writes `status=skipped` and does not update `last_successful_*` aggregates. Remote-ingest-only failure writes `status=ok_with_remote_ingest_failure`. |
| `tests/test_sync_status.py` | Status report matches recorded runs. Mirror divergence (manifest hash mismatch) raises a warning. |
| `tests/test_sync_conflicts.py` | Two docs with same `source_path` but different `origin_node_id` appear in conflicts list as advisory. |
| `tests/test_scheduler_launchd.py` | `install-sync` renders plist labeled `com.pkm-brain.sync-primary` with `StartInterval=1800` and command `brain sync run <peer> --if-reachable`. `install-secondary-capture` renders `com.pkm-brain.capture-secondary` calling `brain automation secondary-tick`. `uninstall-*` removes only the target label. |
| `tests/test_scheduler_stubs.py` | `systemd.Scheduler().install(...)` raises `NotImplementedError` with documented message. Same for `cron`. |

### 7.4 Validation checks

```bash
# 1. Tests
uv run pytest tests/test_sync_runs_schema.py tests/test_sync_run_recording.py \
  tests/test_sync_status.py tests/test_sync_conflicts.py \
  tests/test_scheduler_launchd.py tests/test_scheduler_stubs.py -q

# 2. sync_runs schema
sqlite3 ~/brain/db/brain.sqlite "PRAGMA table_info(sync_runs);" \
  | awk -F'|' '{print $2}' | sort > /tmp/cols.txt
for c in id peer_node_id direction started_at finished_at status \
  files_pulled files_pushed bytes_pulled bytes_pushed \
  primary_ingest_run_id remote_ingest_status errors; do
  grep -q "^$c\$" /tmp/cols.txt || { echo "missing column: $c"; exit 1; }
done

# 3. Scheduler CLI present
uv run brain scheduler install-sync --help | grep -q -- --peer
uv run brain scheduler install-secondary-capture --help | grep -q -- --interval

# 4. LaunchAgent label
uv run brain scheduler install-sync --peer secondary-test --interval 1800 --dry-run \
  | grep -q "com.pkm-brain.sync-primary"
uv run brain scheduler install-secondary-capture --interval 600 --dry-run \
  | grep -q "com.pkm-brain.capture-secondary"
```

---

## 8. Milestone M5 — Installer wizard + local Web UI

### 8.1 Goals

- `brain setup` is the documented user-facing install path; it composes M1–M4 commands.
- `brain ui` provides a loopback-only control plane backed by the M1 service layer.

### 8.2 Implementation steps

**M5.1 — `brain setup` wizard**

- `brain setup` (and `brain init --wizard` alias) implements spec §10.3 step list.
- Supports `--dry-run` (prints planned writes, writes nothing) and `--json` (machine-readable plan).
- Never prompts for or persists private keys, passwords, or tokens. Identity files referenced by path; host keys via fingerprint.
- Wizard fails closed: `sync doctor` or `test-connection` failure blocks scheduler install but does not roll back already-validated single-machine setup.

**M5.2 — Web UI**

- `brain ui --host 127.0.0.1 --port 8765` — FastAPI server bound to loopback by default.
- Local auth token: generated at first run, stored at `config/local/ui_token` with `0600`. Required as `Authorization: Bearer <token>` header (or signed cookie).
- Pages per spec §16: Status, Setup, Sync, Jobs, Logs, Memory Review.
- Every page calls into the M1 service layer; no separate data model.
- `--host 0.0.0.0` requires `--i-understand-this-binds-to-lan` flag and prints a warning.
- Always-on `brain ui service install/status/uninstall` deferred unless explicitly requested.

### 8.3 Tests to author

| File | Cases |
|------|-------|
| `tests/test_setup_wizard.py` | `--dry-run` writes no files; `--json` plan contains role, node_id, planned LaunchAgent labels, and validation steps. Failed doctor blocks scheduler install. Wizard never writes anything matching a private-key regex. |
| `tests/test_ui_auth.py` | Missing token → 401. Wrong token → 401. Valid token → 200. Token file is `0600`. |
| `tests/test_ui_endpoints.py` | `GET /api/status` returns expected service-layer JSON. `GET /api/memory` lists memories with status filter. `POST /api/memory/<id>/approve` calls the same code path as `brain memory approve` and writes the same memory file (M1). |
| `tests/test_ui_bind.py` | Default bind is `127.0.0.1`. `--host 0.0.0.0` without the flag refuses; with the flag prints a warning and binds. |

### 8.4 Validation checks

```bash
# 1. Tests
uv run pytest tests/test_setup_wizard.py tests/test_ui_auth.py \
  tests/test_ui_endpoints.py tests/test_ui_bind.py -q

# 2. Wizard dry-run touches nothing
ls /tmp/brain-m5/ 2>/dev/null && rm -rf /tmp/brain-m5
uv run brain --home /tmp/brain-m5 setup --dry-run --json \
  | python -c "import json,sys; p=json.load(sys.stdin); assert 'planned_writes' in p"
test ! -e /tmp/brain-m5/config/sync.yaml

# 3. UI token enforced
uv run brain ui --port 18765 &
UI_PID=$!
sleep 1
test "$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:18765/api/status)" = "401"
TOKEN=$(cat ~/brain/config/local/ui_token)
test "$(curl -s -o /dev/null -w '%{http_code}' \
  -H "Authorization: Bearer $TOKEN" \
  http://127.0.0.1:18765/api/status)" = "200"
kill $UI_PID

# 4. LAN bind refused without override flag
! uv run brain ui --host 0.0.0.0 --port 18766 --dry-run 2>/dev/null
uv run brain ui --host 0.0.0.0 --port 18766 \
  --i-understand-this-binds-to-lan --dry-run | grep -qi warning
```

---

## 9. Ingestion failure matrix (cross-milestone reference)

| Failure point                          | Behavior                                                                                                                                                | `sync_runs.status`              | Side effects                                                                                                              |
|----------------------------------------|---------------------------------------------------------------------------------------------------------------------------------------------------------|---------------------------------|---------------------------------------------------------------------------------------------------------------------------|
| Secondary unreachable (`--if-reachable`) | Exit 0; log "skipped: peer unreachable".                                                                                                              | `skipped`                       | No push. Last-successful-pull unchanged.                                                                                  |
| Secondary unreachable (no flag)        | Exit non-zero.                                                                                                                                          | `failed`                        | No push. `errors` records SSH return.                                                                                     |
| SSH OK, remote `sync doctor` fails     | Exit non-zero. Do not run rsync.                                                                                                                        | `failed`                        | `errors` carries remote doctor JSON.                                                                                      |
| Rsync pull partial / non-zero          | Live inbox untouched (staging design). Staging dir preserved for inspection.                                                                            | `failed`                        | Next run starts a fresh staging dir.                                                                                      |
| Manifest hash mismatch on pulled file  | That file moved to `_staging/<run_id>/_rejected/` with `.error.json`. Siblings promoted normally.                                                       | run continues; file flagged     | Prevents ingesting tampered/truncated transfers.                                                                          |
| Pull OK, **Primary ingest** fails      | Do not push. Promoted files stay in live external inbox so a later `brain ingest --retry-quarantine` can retry.                                         | `failed`                        | `last_successful_pull` may update (bytes on disk); push blocked.                                                          |
| Per-document ingest failure            | File moved to `inbox/external/<peer>/_quarantine/<rel>` + `.error.json`. Ingest continues for siblings.                                                 | run can still be `ok`           | `brain ingest --retry-quarantine` re-attempts. Nightly job retries on schedule.                                           |
| Push partial / non-zero                | Stop. Do not run remote ingest. Mirror is "degraded".                                                                                                   | `failed`                        | `brain sync status` shows mirror-divergence warning until next clean push.                                                |
| Push OK, **remote ingest** fails       | Sync-level success, remote ingest failed.                                                                                                               | `ok_with_remote_ingest_failure` | Next `sync run` retries remote ingest before pushing again.                                                               |

Cross-cutting:

- Every failure writes a `sync_runs` row before exiting — there is no silent failure.
- Failed runs never decrement or rewrite the `last_successful_*` aggregates.
- Quarantined / rejected files are never deleted by sync; cleanup is an explicit `brain sync clean-quarantine` action.
- Ingest mutations to `raw/` or `wiki/` only happen after a successful per-document ingest (existing property; must not regress).

---

## 10. Installation process (end-state, after M5)

### 10.1 Primary (laptop)

```bash
git clone <pkm-brain-repo> && cd pkm-brain
uv sync
brew install rsync  # or equivalent
uv run brain setup
  # 1. brain_home (default ~/brain)
  # 2. Local init: raw/ wiki/ memory/ inbox/ db/ indexes/ logs/ config/local/ config/shared/
  # 3. "Set up multi-device sync?" -> yes
  # 4. role=primary; prompt node_id
  # 5. "Add a Secondary now?" -> prompt node_id, host, user, remote brain_home, identity path
  # 6. brain sync doctor
  # 7. brain sync test-connection <secondary>
  # 8. Offer: brain scheduler install-sync --peer <secondary> --interval 1800
```

### 10.2 Secondary (LAN-only desktop)

```bash
git clone <pkm-brain-repo> && cd pkm-brain
uv sync
uv run brain setup
  # 1. brain_home
  # 2. Local init (same dirs as Primary)
  # 3. role=secondary; prompt node_id, expected primary node_id, outbox path
  # 4. Select local capture sources (codex, claude, opencode, hyprnote)
  # 5. brain sync doctor
  # 6. Offer: brain scheduler install-secondary-capture --interval 600
# System-level: ensure sshd accepts the Primary's key for the configured user
```

### 10.3 Install-time guarantees

- `--dry-run` and `--json` are supported on every wizard step.
- Wizard never writes secrets; only paths and fingerprints.
- Failed `sync doctor` or `test-connection` blocks scheduler install but does not undo local setup.
- After M5, the Web UI exposes the same flow at `http://127.0.0.1:8765`.

---

## 11. Cross-milestone validation (run before tagging V1)

```bash
# 1. Full test suite + lint
uv run pytest -q
uv run ruff check .

# 2. Schema audit
sqlite3 ~/brain/db/brain.sqlite "SELECT version, applied_at FROM schema_migrations ORDER BY version;"
# Expect at least: 001 (origin identity), 002 (sync_runs).

# 3. Spec drift audit — confirm no "Not yet implemented" item is still missing in code
uv run python -c "
import subprocess
required = [
  'brain setup', 'brain sync init-primary', 'brain sync init-secondary',
  'brain sync add-peer', 'brain sync doctor', 'brain sync test-connection',
  'brain sync pull', 'brain sync push', 'brain sync run', 'brain sync status',
  'brain sync conflicts', 'brain scheduler install-sync',
  'brain scheduler install-secondary-capture', 'brain scheduler status',
  'brain ui', 'brain memory export-all', 'brain memory import',
  'brain automation secondary-tick',
]
help_out = subprocess.run(['uv','run','brain','--help'], capture_output=True, text=True).stdout
# At minimum the top-level groups should be discoverable
for cmd in ['sync', 'scheduler', 'setup', 'ui']:
    assert cmd in help_out, cmd
print('top-level groups present')
"

# 4. Manual acceptance: run spec §17 acceptance flow once and check off each step.
# Capture results in docs/runbooks/sync-acceptance.md.
```

---

## 12. Open items (decisions deferred to implementation)

These are flagged inline in the spec §18. The plan resolves the first three at landing time:

| Question | Plan resolution |
|----------|-----------------|
| Export reviewed memories as Markdown vs SQLite-only? | **Markdown export** — added in M1.3. `memory/` is plain files, mirror-able by rsync. SQLite remains canonical. |
| Remote ingest cadence after push? | **After every successful push** (M3.7). Revisit if push frequency increases. |
| Web UI framework? | **FastAPI** (M5.2). Typed endpoints, low ceremony, mirrors service layer cleanly. |
| Should Secondary import structured `agent_sessions` records, or only Markdown? | Defer to post-V1. Markdown is sufficient for retrieval. |
| Secondary read-only MCP mode? | Defer. Current MCP server works against local SQLite; Secondary will surface its mirror naturally. |
| Default topology: hostnames, static IPs, or both? | Spec accepts both; `add-peer` accepts either. No further work needed. |
| Always-on UI service in V1? | **Deferred.** On-demand only in M5; `brain ui service install` not implemented until requested. |
