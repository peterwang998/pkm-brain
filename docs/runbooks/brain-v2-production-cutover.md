# Brain V2 Production Cutover

**Status:** deployed in place; scheduler resumed; bounded backfills active
**Last verified:** 2026-07-26 against Brain `0.2.2` build 8 at commit `c952419`

The live evidence and remaining follow-ups are recorded in
[`brain-v2-production-deployment-record.md`](brain-v2-production-deployment-record.md).

## Decision And Boundary

Upgrade `/Users/Peter/brain` in place. Do **not** replace it with
`/Users/Peter/brain-v2`: that experimental home is not a complete production
Brain because it has no Operations database or Gmail archive and contains a
large superseded Gmail test population.

This release preserves the Original Brain corpus and adds migrations 24-26,
Gmail Knowledge ingestion, temporal-ledger infrastructure, extraction
compatibility, and process isolation for mutation-heavy scheduled work. It does
not promote Gmail temporal review into trusted facts or actions.

The fixed privacy boundary is:

- Gmail network access is limited to the Gmail provider API. Encrypted archive
  storage, classification, and Knowledge projection remain local; no model is
  part of these scheduled steps.
- `source_types.gmail_thread.extract` is `false` and `full_coverage` is `false`.
  Private Gmail must not enter the normal external-LLM fact extractor.
- The Gmail temporal verifier is not a scheduled production job. The new
  temporal ledger begins empty and remains review-only unless a separately
  authorized local/private evaluation is run.
- Existing successful extraction receipts from
  `extractor-evidence-units-v5` and
  `extractor-evidence-units-v6-speaker-context` remain terminal. Failed,
  invalid, and empty work can replay under the current extractor. In the live
  rehearsal this bounded current-version replay to 37 `hyprnote_meeting`
  documents rather than reprocessing the corpus.

## Verified Rehearsal Evidence

The full-home clone
`/Users/Peter/Documents/Codex/production-migration-backups/brain-20260726T0706Z-v2-rehearsal`
was migrated with the scheduler disabled and without provider or model calls.

| Check | Rehearsal result |
|---|---|
| Knowledge schema | exact prefix 1-23 became exact prefix 1-26 |
| Operations/archive schemas | remained 9 and 2 |
| SQLite integrity / foreign keys | `ok` / zero violations |
| Documents / active documents | 447 / 447, unchanged |
| Chunks / FTS rows | 5,241 / 5,241, unchanged |
| Facts / active facts | 5,361 / 2,710, unchanged |
| Entities / fact links / relations | 638 / 2,809 / 0, unchanged |
| Open questions / curation actions | 1,454 / 6,173, unchanged |
| Memories / Wiki pages | 10 / 1,120, unchanged |
| Operations items / observations / events / cursors | 166 / 168 / 172 / 2, unchanged |
| Five new temporal-ledger tables | all empty |
| Lexical retrieval | exact pre/post fingerprints for four non-empty queries |
| Doctor | SQLite, vector index, and embedding checks passed; only the pre-existing old-nightly warning remained |

A stopped-app diagnostic using the new Gmail archive code also succeeded:
153 changes fetched, 143 inserted, 10 updated, 14 deleted, and no skipped or
reported error. A no-write Gmail Knowledge rehearsal discovered 7,256 active
archive threads and prepared 100 of a 100-item batch with zero errors. The
remaining 7,156 count is a batch-bound skip count, **not** evidence that ads or
low-value mail were semantically rejected.

## Release Artifact Gate

Build from a clean worktree at the intended release commit. Do not include
unrelated local app edits.

```bash
cd /Users/Peter/Documents/Codex/2026-07-14/what/work/pkm-brain-temporal

.venv/bin/ruff check .
.venv/bin/pytest -q
swift test --package-path app
scripts/m3-migration-acceptance.sh
scripts/m2-clean-machine-acceptance.sh "$(mktemp -d "${TMPDIR:-/tmp}/pkm-brain-m2-v2.XXXXXX")"
```

The gate is green only when all commands pass and the clean build produces a
validly signed `dist/PKM Brain.app` with app and Python package version `0.2.2`.

## Exact Cutover Procedure

### 1. Freeze and record the old runtime

Extend the scheduler pause to seven days so both the new app and a restored old
home remain quiet throughout the cutover/rollback window. Record `/api/health`,
`/api/version`, `/api/scheduler`, the app version/build, the app-managed runtime
target, and the current `brain`/`pkm-brain` shell targets. The expected old app
is `0.1.6` build 5 with runtime `0.1.6-12a45456-7adaae5c`.

```bash
/Users/Peter/.local/bin/pkm-brain-curl /api/scheduler/pause \
  -H 'Content-Type: application/json' -d '{"seconds":604800}'
/Users/Peter/.local/bin/pkm-brain-api-get /api/health
/Users/Peter/.local/bin/pkm-brain-api-get /api/version
/Users/Peter/.local/bin/pkm-brain-api-get /api/scheduler

osascript -e 'tell application id "com.pkm-brain.app" to quit'
lsof /Users/Peter/brain/db/brain.sqlite \
     /Users/Peter/brain/db/ops.sqlite \
     /Users/Peter/brain/mail/gmail-archive.sqlite
```

Do not continue until the app is stopped and `lsof` reports no handles for all
three databases.

### 2. Make the final full-home rollback snapshot

The earlier verified snapshot is a safety net, but it predates the latest Gmail
archive catch-up. Create a new quiescent clone immediately before cutover.

```bash
umask 077
export BRAIN_HOME=/Users/Peter/brain
export BACKUP_ROOT=/Users/Peter/Documents/Codex/production-migration-backups
export CUTOVER_STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
export FULL_SNAPSHOT="$BACKUP_ROOT/brain-$CUTOVER_STAMP-final-pre-v2"

cp -cR "$BRAIN_HOME" "$FULL_SNAPSHOT"
chmod -R go-rwx "$FULL_SNAPSHOT"

sqlite3 "$FULL_SNAPSHOT/db/brain.sqlite" 'PRAGMA integrity_check; PRAGMA foreign_key_check;'
sqlite3 "$FULL_SNAPSHOT/db/ops.sqlite" 'PRAGMA integrity_check; PRAGMA foreign_key_check;'
sqlite3 "$FULL_SNAPSHOT/mail/gmail-archive.sqlite" 'PRAGMA quick_check;'
shasum -a 256 "$FULL_SNAPSHOT/db/brain.sqlite" \
               "$FULL_SNAPSHOT/db/ops.sqlite" \
               "$FULL_SNAPSHOT/mail/gmail-archive.sqlite"
```

Record the hashes, file count, logical size, owner-only permissions, app/runtime
identity, and schema versions in `$FULL_SNAPSHOT/MIGRATION-SNAPSHOT.json`.
Direct database inspection must show Knowledge schema 23, Operations schema 9,
archive schema 2, `integrity_check=ok`, and no foreign-key rows. The old app may
report schema 21 through its API; that reflects the old runtime's compiled
schema head and does not override the database's exact migration ledger.

Also create and exercise the release's narrower coordinated database-pair
recovery set. This is an additional check, not a substitute for the full-home
snapshot: it excludes Gmail archive/mirror, sources, Wiki, indexes,
configuration, and logs.

```bash
export RELEASE_BRAIN=/path/to/clean-release-worktree/.venv/bin/brain
export PAIR_RECOVERY="$BACKUP_ROOT/brain-$CUTOVER_STAMP-db-pair"
export ISOLATED_RESTORE="$BACKUP_ROOT/brain-$CUTOVER_STAMP-db-pair-restore"

"$RELEASE_BRAIN" recovery create --home "$BRAIN_HOME" --output "$PAIR_RECOVERY"
"$RELEASE_BRAIN" recovery verify "$PAIR_RECOVERY"
"$RELEASE_BRAIN" recovery restore-isolated "$PAIR_RECOVERY" "$ISOLATED_RESTORE"
```

The isolated target must not exist beforehand. Do not start a daemon against
it; verification must not initialize or migrate the restored databases.

### 3. Install the reviewed configuration and app

The staged files are exact copies of live configuration with only the reviewed
cutover changes. Install them while Brain is stopped, preserve owner-only
permissions, and re-check the privacy invariants before launching.

```bash
install -m 600 \
  /Users/Peter/Documents/Codex/production-migration-backups/cos_llm-v2-cutover.yaml \
  /Users/Peter/brain/config/local/cos_llm.yaml
install -m 600 \
  /Users/Peter/Documents/Codex/production-migration-backups/connectors-v2-cutover.yaml \
  /Users/Peter/brain/config/local/connectors.yaml

cd /path/to/clean-release-worktree
scripts/install-app.sh --activate
```

Before install, the staged extraction configuration must list only the two
compatible terminal versions above and must disable both extraction and full
coverage for `gmail_thread`. The staged connectors configuration must enable
Gmail. Run the installer once: it retains the previous app at
`/Applications/.PKM Brain.app.previous`.

### 4. Validate the schema-only phase while still paused

Do not run any scheduled job until every item below passes:

1. `/api/health` reports `ok=true`, version `0.2.2`, schema 26, the expected
   runtime ID, and home `/Users/Peter/brain`.
2. The migration ledger is the exact contiguous prefix 1-26; Knowledge and
   Operations integrity checks pass with no foreign-key rows; the archive
   quick check passes.
3. Every legacy count in the rehearsal table still matches. The five temporal
   ledger tables are empty and chunk/FTS counts both equal 5,241.
4. `brain doctor --home /Users/Peter/brain` passes the database, vector, and
   embedding checks. The same four lexical probes retain their fingerprints.
5. Both the installed and previous app bundles pass `codesign --verify --deep --strict`.
6. The app-managed shim reports `brain 0.2.2`; align
   `/Users/Peter/.local/bin/brain` (and `pkm-brain`, if present) to that shim
   only after preserving their old targets in the migration backup.
7. An MCP read round trip succeeds through the app-managed runtime.
8. No legacy Brain LaunchAgent is loaded.
9. `/api/scheduler` remains paused and shows the expected seven jobs for this
   primary: `capture_tick`, `nightly`, `gmail_mirror_sync`,
   `gmail_archive_sync`, `gmail_knowledge_ingest`, `meeting_preparation`, and
   `sync:Peters-Mac-mini`.
10. `capture_tick`, `nightly`, `gmail_knowledge_ingest`, and the sync job report
    `isolated=true` and lane `knowledge_mutation`. Gmail mirror and archive
    remain serialized on lane `provider_sync`. No private child exception text
    may appear in scheduler results.

### 5. Run the Gmail canary locally

Run one job at a time while the scheduler remains paused; run-now intentionally
bypasses the pause. Poll `/api/scheduler` until each job is no longer running
before starting the next.

```bash
API=/Users/Peter/.local/bin/pkm-brain-curl

"$API" /api/scheduler/run -H 'Content-Type: application/json' \
  -d '{"job_id":"gmail_archive_sync"}'
"$API" /api/scheduler/run -H 'Content-Type: application/json' \
  -d '{"job_id":"gmail_mirror_sync"}'
"$API" /api/scheduler/run -H 'Content-Type: application/json' \
  -d '{"job_id":"gmail_knowledge_ingest"}'
```

During `gmail_knowledge_ingest`, repeatedly call `/api/health`. It must remain
responsive while the isolated child runs. Accept the canary only if archive and
mirror sync complete without corruption, Knowledge ingestion reports zero
errors and zero held documents, active/superseded revisions reconcile, and no
Gmail extraction receipt, Gmail fact, temporal-verifier execution, or temporal
ledger row is created. Inspect local aggregate importance/routing counts before
claiming that ads and non-important updates are filtered sufficiently; the
rehearsal batch ratio alone cannot support that claim.

### 6. Resume and observe

```bash
"$API" /api/scheduler/resume -H 'Content-Type: application/json' -d '{}'
```

Observe for at least 60 minutes, covering several ten-minute Gmail/capture
cadences and one nightly scheduler check. The daemon PID and `started_at` must
remain stable, health and MCP reads must remain responsive during child work,
jobs must not overlap within their lanes, and no child process may survive app
shutdown. Record the final scheduler, archive, document/fact, temporal-ledger,
and storage aggregates in the cutover record.

## Rollback Triggers

Rollback immediately before resuming if any schema, integrity, count-parity,
privacy-fence, runtime-identity, code-signing, scheduler-topology, or MCP check
fails. After resuming, rollback for any of the following:

- a private Gmail provider/model boundary violation or any automatic Gmail
  temporal/fact promotion;
- repeated daemon restart, sustained API unresponsiveness, a native crash, or a
  child that is not terminated with the app;
- database corruption, a migration gap, archive key/decryption failure, or
  unreconciled destructive Gmail projection;
- scheduler jobs running in the wrong lane/process, repeated unexplained job
  failure, or reappearance of a legacy LaunchAgent; or
- material Original Brain retrieval/fact regression that was absent from the
  schema-only rehearsal.

One bounded Gmail provider failure is not by itself database corruption, but
leave the scheduler paused and investigate; do not weaken the privacy fence to
make the job pass.

## Exact Rollback Procedure

Pause first if the API is responsive, then stop the app and quiesce the three
databases:

```bash
/Users/Peter/.local/bin/pkm-brain-curl /api/scheduler/pause \
  -H 'Content-Type: application/json' -d '{"seconds":86400}'
osascript -e 'tell application id "com.pkm-brain.app" to quit'
lsof /Users/Peter/brain/db/brain.sqlite \
     /Users/Peter/brain/db/ops.sqlite \
     /Users/Peter/brain/mail/gmail-archive.sqlite
```

Do not continue until `lsof` is empty. Supply the exact final snapshot path
recorded during cutover, preserve the failed state, restore the full home, and
swap back the retained app:

```bash
umask 077
export ROLLBACK_STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
export BACKUP_ROOT=/Users/Peter/Documents/Codex/production-migration-backups
export FULL_SNAPSHOT=/exact/path/recorded-at-cutover
export FAILED_HOME="$BACKUP_ROOT/brain-$ROLLBACK_STAMP-failed-v2"
export FAILED_APP="/Applications/.PKM Brain.app.failed-v2-$ROLLBACK_STAMP"

mv /Users/Peter/brain "$FAILED_HOME"
cp -cR "$FULL_SNAPSHOT" /Users/Peter/brain
chmod -R go-rwx /Users/Peter/brain

mv "/Applications/PKM Brain.app" "$FAILED_APP"
mv "/Applications/.PKM Brain.app.previous" "/Applications/PKM Brain.app"
open -n "/Applications/PKM Brain.app"
```

Restore the shell-command targets saved during preflight. Leave the restored
old scheduler paused. Verify old app/runtime identity, direct schema ledgers,
all three database integrity checks, archive Keychain readability, corpus
counts, retrieval, MCP, and absence of legacy LaunchAgents before resuming old
automation. Do not use the database-pair recovery set as a full rollback.

The earlier independently verified fallback remains
`/Users/Peter/Documents/Codex/production-migration-backups/brain-20260726T0706Z-pre-v2`.
Its database hashes at creation were:

- `brain.sqlite`: `7ef57d563ef5ad4d2e3be0156e925508b6c562b2cb1d93ef1e0480a199cb92c0`
- `ops.sqlite`: `84fb6532b48d24e466ceded69698330867541e2a2bc8c5a25ab1f4666667a802`
- `gmail-archive.sqlite`: `3eccf95ecacbebe96a08d2cf6da34601ec3d322a8239d8151fca7e2d62d76f5d`

That fallback predates the successful archive catch-up, so prefer the final
quiescent snapshot and let Gmail history resynchronize only if the older
fallback is required.

## Completion Record

The migration is complete only after recording: release commit and runtime ID;
final snapshot path and hashes; pre/post schema and legacy-count parity; test
and signing results; config/privacy assertions; controlled Gmail job results;
MCP and retrieval checks; 60-minute stability observations; and either the
exact resume time or the rollback trigger and restored snapshot.
