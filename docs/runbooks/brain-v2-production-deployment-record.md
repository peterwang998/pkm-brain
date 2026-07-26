# Brain V2 Production Deployment Record

**Status:** deployed and resumed
**Deployment date:** 2026-07-26
**Live release:** Brain `0.2.2`, app build 8
**Release commit:** `c9524199f79a2041859151abbb1f015911a4147a`
**Live home:** `/Users/Peter/brain`

## Release Lineage

- `6f33d4f`: prepared the Brain V2 schema, temporal architecture, Gmail
  connector, scheduler isolation, privacy boundaries, and migration tooling.
- `df7dbe5`: made agent capture lazy and bounded scheduled ingestion to 16
  changed documents and 64 MiB of changed source material per tick.
- `c952419`: replaced recursive LanceDB `OR` deletion predicates with escaped,
  bounded `IN` predicates and added a real-Lance regression at the live crash
  cardinality.

The installed app and app-managed Python runtime both report `0.2.2`. The live
runtime ID is `0.2.2-12a45456-c72b7ae6`, and the daemon reports Knowledge schema
26.

## Rollback And Recovery

The quiescent pre-V2 full-home snapshot is:

`/Users/Peter/Documents/Codex/production-migration-backups/brain-20260726T075302Z-final-pre-v2`

Its manifest records an exact Knowledge schema 1-23 ledger, Operations schema
1-9 ledger, Gmail archive schema 2, passing integrity checks, database hashes,
owner-only permissions, and the old app/runtime identity.

The separately verified coordinated database recovery set is:

`/Users/Peter/Documents/Codex/production-migration-backups/brain-20260726T075302Z-db-pair`

Recovery set `recovery_5a779d01202d45c1` was verified and restored to an
isolated quarantined home without starting a daemon.

## Release Gates

All gates ran from the clean detached worktree
`/Users/Peter/Documents/Codex/brain-v2-release-c952419`:

| Gate | Result |
|---|---|
| Python suite | 2,787 passed |
| Ruff | passed |
| Swift suite | 28 passed |
| Signed app build | passed |
| M2 clean-machine install/restart | passed |
| M3 migration acceptance | passed |
| App version/build | `0.2.2` / 8 |
| Wheel | `pkm_brain-0.2.2-py3-none-any.whl` |
| Wheel SHA-256 | `c72b7ae6fc1dc830ed120262e7e6eaf3bf184084c5d1bf4898a8aa5a7c1fa9bc` |

The installed bundle, release bundle, and retained previous bundle pass strict
deep code-sign verification. Both shell shims resolve through the app-managed
runtime and report `0.2.2`.

## Data And Privacy Validation

The Knowledge migration ledger is the exact contiguous set 1-26. Operations
remains the exact set 1-9, and the Gmail archive remains schema 2. Knowledge and
Operations integrity checks pass with no foreign-key violations; the archive
quick check passes.

The successful bounded capture canary left:

| Aggregate | Post-capture canary value |
|---|---:|
| Documents / active documents | 961 / 891 |
| Chunks / active chunks | 6,952 / 6,882 |
| Active chunk FTS rows | 6,882 |
| Active Lance vectors | 6,882 |
| Facts / active facts | 5,361 / 2,710 |
| Entities / fact links | 638 / 2,809 |
| Operations items / observations / events / cursors | 166 / 168 / 172 / 2 |

The legacy facts, entities, fact links, and Operations counts are unchanged.
Index doctor reports zero missing and zero stale vectors.

After the first normally scheduled Gmail Knowledge batch, the checkpoint totals
were 1,461 documents, 1,358 active documents, 8,088 chunks, and 7,974 active
chunks. Active FTS and LanceDB both contained the same 7,974 chunk IDs, with
zero missing and zero stale vectors.

Private Gmail remains fail-closed for external extraction:

- `source_types.gmail_thread.extract` is `false`.
- `source_types.gmail_thread.full_coverage` is `false`.
- All five Gmail temporal-review ledger tables are empty.
- No Gmail fact extraction or temporal-review job is scheduled.

The initial pre-resume Gmail Knowledge batch retained 430 active and 70 deleted
document revisions. Its active routing distribution was 4 action, 312
informational, 99 promotional, and 15 time-sensitive documents. Generic
retrieval suppression applied to 393 of 430 active documents. These are local
classification and retrieval controls, not external model extraction.

## Capture Failure, Root Cause, And Corrective Canary

The first `0.2.1` live capture canary isolated a latent Original Brain scale
defect. Capture completed, but ingestion attempted to delete 668 stale vectors
using one left-deep `OR` predicate. LanceDB 0.30.2 succeeds at 658 operands and
reproducibly exits with SIGBUS at 659. A prior native report for the same
unchanged LanceDB binary shows a stack-guard fault after 666 recursive frames.
macOS did not persist a fresh native stack for the live PID because its report
limit was exceeded, so the stack match is a very high-confidence inference
rather than a new PID-specific backtrace.

The failed isolated run left SQLite intact but removed 1,321 agent-session
vectors before SQLite could commit. Brain `0.2.2` repaired those vectors with a
missing-only rebuild. The exact workload then succeeded:

- 651 agent sessions discovered and 11 current Codex sessions refreshed;
- 16 changed documents ingested and 2 older snapshots replaced;
- 1,326 chunks and 1,326 embeddings written;
- zero capture, ingest, or vector-write errors;
- daemon PID and start time remained stable;
- SQLite, FTS, and LanceDB all converged to 6,882 active chunk IDs.

Lance maintenance then compacted 990 retained versions to one version and one
data file, reclaiming 416,827,767 bytes. Final index doctor status is `ok`.

## Scheduler State

The scheduler is resumed with seven jobs:

- `capture_tick`, `gmail_knowledge_ingest`, `nightly`, and
  `sync:Peters-Mac-mini` are isolated on the `knowledge_mutation` lane.
- `gmail_archive_sync` and `gmail_mirror_sync` are serialized on
  `provider_sync`.
- `meeting_preparation` remains on the serial lane.

The first resumed Gmail mirror tick completed one incremental update with no
error. The first archive tick fetched 129 messages, inserted 119, updated 10,
deleted 10, and stopped normally at its bounded partial-coverage checkpoint.
The first normally scheduled Gmail Knowledge tick then captured and ingested
500 revisions, wrote 1,136 embeddings, and reconciled to 897 active, 100
deleted, and 3 superseded Gmail documents with zero errors and zero held
documents. The following automatic `capture_tick` also completed successfully:
16 documents and 411 embeddings were written with zero errors. Its post-run
index doctor found 8,385 active SQLite chunks and 8,385 Lance vectors, with
zero missing and zero stale vectors.

## Continuing Work

- Gmail archive history and Gmail Knowledge projection remain incomplete and
  will continue in bounded scheduled batches.
- At resume, 453 changed or unindexed Codex artifacts remained. They will drain
  incrementally instead of entering one unbounded transaction.
- Gmail temporal review remains evaluation-only. Production does not yet
  promote Gmail temporal hypotheses into facts, events, reminders, or actions.
- Lance and SQLite are separate stores. Deletes above 1,024 IDs may still span
  multiple Lance commits before SQLite commits; index doctor and missing-only
  repair provide the recovery path, but cross-store atomicity is a later
  hardening item.
- Cold-cache agent capture can still peak near 2 GiB; the production path is
  isolated, provider sessions are rejected before rollout reads, active
  rollouts are semantically bounded, and scheduled work is byte/document
  limited.
