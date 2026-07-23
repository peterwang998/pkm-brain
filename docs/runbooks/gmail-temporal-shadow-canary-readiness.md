# Gmail Temporal Shadow Canary Readiness

**Status:** structurally runnable in an isolated Brain home; semantic canary launch is blocked

**Last verified:** 2026-07-23 with the public synthetic ledger smoke passing, plus nine focused shadow-audit tests and seven ledger-smoke tests passing

## Decision

Do not start the production-claim canary yet. The current system can safely run
a content-free, preparation-only shadow over incremental Gmail Knowledge
revisions and can distinguish:

- messages first seen in a new thread; and
- message-unseen additions to a thread that existed at canary baseline.

It cannot yet execute or score the semantic canary end to end. There is no
scheduled or CLI temporal-verifier/runner executor. Pure fail-closed event
identity and thread-lifecycle projections now exist, but there is no provider
executor, durable identity-resolution ledger, or runner integration. There is
also no durable end-to-end freshness
clock and no owner-label path that turns a candidate-bearing proxy into the
required material-case and precision/recall denominators. These are fail-closed
blockers, not metrics to infer from structural preparation.

## Public Synthetic Ledger Smoke

The repository has one runnable, aggregate-only smoke for the temporal ledger
boundary. It uses a public synthetic Gmail Knowledge document in a new,
explicitly marked root and a non-production pipeline scope. It makes no Gmail,
network, provider, or model call.

The outer root and inner Brain home carry a nonce-bound marker pair. Before any
resume ledger or source read, the smoke rejects overlap with the configured
production Brain home and rejects symlinked, hard-linked, foreign-owned, or
otherwise redirected members in the synthetic home.

Run it with a parent that may exist and a child smoke root that must not:

```bash
cd /Users/Peter/Documents/Codex/2026-07-14/what/work/pkm-brain-temporal

SMOKE_PARENT="$(mktemp -d "${TMPDIR:-/tmp}/brain-temporal-ledger.XXXXXX")"
SMOKE_ROOT="$SMOKE_PARENT/run"

.venv/bin/python scripts/smoke_gmail_temporal_ledger_public.py run \
  --root "$SMOKE_ROOT"
```

`run` persists one valid deferred event-time review projection and then invokes
the script's `resume` command in a fresh Python process. A passing report proves
all of the following against the production ledger and recovery library APIs:

- the first projection creates one immutable run, one artifact, and one mutable
  head at generation 1;
- exact replay after a fresh interpreter creates no new or changed temporal
  rows;
- a stale compare-and-swap advance fails without leaving a candidate run or
  artifact behind;
- head rollback, idempotent rollback replay, source-bound clear, and a
  post-clear advance produce the expected generations through generation 5;
- superseding the source document makes the head explicitly stale and prevents
  restoring an older run;
- a coordinated knowledge/operations database-pair recovery set verifies, an
  isolated restore contains identical rows across all five temporal ledger
  tables, and the restored home remains quarantined with no daemon start; and
- stdout contains aggregate checks and counts only. It contains no source text,
  provider identity, message identity, run ID, message-scope key, or local path.

This smoke does **not** prove:

- Gmail provider sync, encrypted-archive append, Knowledge capture, shadow
  observation, or the full sequence between those systems;
- production-scope runner execution receipts, three genuine external verifier
  invocations, or invocation independence—the expected execution and component
  row counts are zero and `independent_invocations_verified=false`;
- temporal extraction semantics, event identity, lifecycle correctness,
  precision, recall, private-data behavior, or freshness latency;
- recovery of source files or a complete Brain home—the coordinated recovery
  primitive covers the database pair, and this smoke asserts the temporal rows
  within that boundary; or
- crash recovery from a process killed inside a transaction, a temporal
  scheduler, a durable canary cycle receipt, or an operator-facing rollback
  command.

The smoke therefore closes the isolated temporal-ledger integration gap. It
does not change this runbook's semantic-canary no-go decision.

## Which Gmail Store Drives This Canary

The operational Gmail mirror and the encrypted Gmail archive are separate.
Both use Gmail history incrementals, but the temporal Knowledge path reads the
encrypted archive, not `cache/gmail-mirror/gmail-mirror.sqlite`. A fresh
operational-mirror checkpoint does not prove that the archive, Knowledge
projection, or temporal preparation is fresh.

The intended chain is:

```text
Gmail history
  -> encrypted archive (provider sync, about every 10 minutes)
  -> immutable Gmail Knowledge thread revision (local projection)
  -> active revision reconciliation
  -> message-local temporal preparation
  -> [missing scheduled verifier/runner execution]
  -> [pure event-identity and lifecycle projection; execution/integration missing]
  -> review-only ledger
```

Archive incrementals retain earlier messages and append a newly fetched message
to the same archive thread. The next Knowledge capture gets a new source
revision, ingests it immutably, marks the prior thread document superseded, and
keeps only the new document active in retrieval. The shadow audit compares
HMAC-pseudonymized message identities, so the earlier messages are not counted
again and the appended message enters `existing_thread_unseen`.

## Preparation-Only Harness

`scripts/audit_gmail_temporal_shadow_canary.py` has three commands:

- `init` freezes the active thread/message population;
- `observe` prepares only messages unseen at baseline and prior observations;
- `status` reports cumulative structural progress without opening Brain.

The state is an owner-only canonical JSON envelope. The HMAC covers the complete
state body—not only a key fingerprint—including its clock mode, generation,
baseline, strata, preparation records, errors, and current revision map. It
contains no Gmail identity, source path, source hash, message text, request
payload, or temporal artifact. Every read verifies the envelope before using
any state field. A failed preparation stays pending and is retried without
increasing the unique-message denominator.

`init`, `observe`, and `status` coordinate through an owner-only adjacent lock
file. `observe` holds the exclusive lock across load, snapshot, preparation, and
commit; the atomic replacement also compares the authenticated prior state and
generation before advancing it. Concurrent observers therefore serialize, and
a stale writer fails rather than losing another observation. `init` refuses to
overwrite an existing state file, so a canary cannot silently redraw its
baseline; retain failed attempts and choose a new explicitly versioned root.

Production duration uses only the process wall clock. `--as-of` is rejected
unless `--non-release-test-clock` is also present. That deterministic mode is
written immutably into the authenticated state: elapsed time remains a
diagnostic, but `seven_days_observed` and release-clock eligibility remain false
forever. Never pass either clock option in the isolated launch sequence below.

Every report asserts:

- zero Gmail provider calls;
- zero external-model calls;
- zero Brain mutations;
- zero temporal-persistence calls;
- no printed private content or request payload; and
- no semantic precision/recall claim.

`candidate_bearing_proxy_messages` is deliberately named a proxy. It is not a
material temporal case and cannot satisfy the 20-case gate.

## Exact Isolated Launch Sequence

The following is the exact preparation-only launch sequence. It has not been
run against the live archive. Use a new, dedicated target home; never point
`CANARY_HOME` at the installed Brain home.

```bash
cd /Users/Peter/Documents/Codex/2026-07-14/what/work/pkm-brain-temporal

export SOURCE_HOME=/Users/Peter/brain
export CANARY_ROOT=/Users/Peter/brain-v2-gmail-canary
export CANARY_HOME="$CANARY_ROOT/home"
export CANARY_KEY="$CANARY_ROOT/shadow-canary.key"
export CANARY_STATE="$CANARY_ROOT/shadow-canary-state.json"

umask 077
mkdir -p "$CANARY_ROOT"
openssl rand 32 > "$CANARY_KEY"
chmod 600 "$CANARY_KEY"

.venv/bin/brain capture gmail \
  --source-home "$SOURCE_HOME" \
  --batch-size all \
  --home "$CANARY_HOME"

.venv/bin/python scripts/audit_gmail_temporal_runner.py \
  --home "$CANARY_HOME"

.venv/bin/python scripts/audit_gmail_temporal_shadow_canary.py init \
  --home "$CANARY_HOME" \
  --state "$CANARY_STATE" \
  --hmac-key "$CANARY_KEY"
```

The first capture must report no capture, ingest, vector-write, reconciliation,
or held-document error before `init`. The full-corpus runner audit must remain
aggregate-only and meet the structural preparation gate. `init` must report
`status=ready` and the expected baseline volume.

The source archive must already be on the current schema with revision digests
populated. Archive discovery can backfill missing legacy digests, so a legacy
archive requires its normal backup/migration procedure before it is used as the
read source for this isolated canary.

One later observation cycle is exactly:

```bash
cd /Users/Peter/Documents/Codex/2026-07-14/what/work/pkm-brain-temporal

.venv/bin/brain capture gmail \
  --source-home "$SOURCE_HOME" \
  --batch-size 500 \
  --home "$CANARY_HOME"

.venv/bin/python scripts/audit_gmail_temporal_shadow_canary.py observe \
  --home "$CANARY_HOME" \
  --state "$CANARY_STATE" \
  --hmac-key "$CANARY_KEY"
```

It may be invoked only after the encrypted archive's provider sync has
completed. Repeating this pair about every ten minutes is not yet an approved
production canary launcher: the repository has no dedicated isolated-canary
scheduler or durable cycle receipt. Until that exists, operator-driven cycles
are useful readiness evidence only.

Cumulative content-free status is:

```bash
.venv/bin/python scripts/audit_gmail_temporal_shadow_canary.py status \
  --state "$CANARY_STATE" \
  --hmac-key "$CANARY_KEY"
```

## Observable Signals

Each Knowledge capture reports capture status and counts, ingest errors, vector
write status, reconciliation errors, held documents, and active/superseded
revision counts. Each shadow observation reports:

- new thread records;
- existing threads with unseen messages;
- revision-only existing-thread updates;
- newly observed messages;
- current preparation failures and static error buckets;
- retries, candidate-bearing proxies, expressions, batches, candidates, and
  pages, separately for both message strata;
- cumulative pending messages, authenticated state generation, and release-clock
  provenance; and
- elapsed-time, 300-message, existing-thread-stratum, and complete-preparation
  structural gates. Test-clock elapsed time is diagnostic only.

The eventual personal-use gate is one clean week, at least 300 newly observed
messages, and at least 20 owner-confirmed material temporal cases. New-thread
and existing-thread results remain separate. The latter is mandatory and a
zero-size existing-thread stratum cannot pass. The current harness can measure
the first two structural quantities and the stratum size. It cannot measure the
20-case semantic denominator, effective recall, confirmed recall, precision,
or lifecycle correctness.

The current stores also do not retain one trusted clock for all four boundaries:
provider change accepted, archive change accepted, Knowledge revision active,
and temporal review ready. Message `internalDate` is an assertion clock, not an
ingestion-latency clock. Therefore the p95 15-minute provider-to-local and
30-minute end-to-end freshness gates are not presently auditable.

## Rollback Boundary

The preparation-only harness writes only the dedicated canary home and
owner-only control artifacts under `CANARY_ROOT`: the HMAC key, authenticated
state envelope, and adjacent state lock. It does not change Gmail, the encrypted
source archive's messages, the installed Brain database, facts, entities,
events, reminders, or the temporal review ledger.

Rollback is therefore:

1. stop the repeating capture/observe process;
2. move `CANARY_ROOT` aside as a retained owner-only failed-run artifact; and
3. continue using the source archive and installed Brain unchanged.

Do not treat temporal-ledger head rollback as available operational recovery.
The library has compare-and-swap rollback and source-bound clear primitives,
and the public synthetic ledger smoke exercises those primitives in its marked
temporary root, but the current canary has no supported CLI around them. The
preparation-only harness intentionally never exercises those writes.

## Fail-Closed Blockers Before Semantic Launch

1. **Temporal execution:** add a bounded scheduler/CLI that launches exactly
   three pinned external verifier invocations per planned page, validates their
   receipts, finalizes through `run_gmail_temporal_review`, and emits only
   aggregate operational status. Private processing still requires explicit
   informed authorization.
2. **Thread lifecycle cognition:** execute and persist the pure, source-bound
   event-identity and lifecycle projections. The identity bridge requires three
   complete external verdict sets, preserves prior event keys across later
   thread revisions, and refuses non-clique or cross-event merges, but no live
   runner currently supplies those verdicts or retains the resulting identity
   authority. Thread membership or a matching date is never enough.
3. **Semantic labels and scoring:** freeze owner-reviewable opaque cases, record
   the material denominator, and score effective/confirmed recall, supported
   precision, lifecycle direction, and critical errors separately for new and
   existing threads. Candidate presence must not stand in for truth.
4. **Durable freshness receipts:** bind provider/archive acceptance, Knowledge
   activation, and temporal completion clocks without using message time as a
   transport clock.
5. **Operational rollback:** expose and test canary-scope disable, stale-head
   clearing, compare-and-swap rollback, restart, idempotent replay, and bounded
   failure quarantine through the actual launcher. The public synthetic smoke
   proves the ledger/recovery subset in isolation, not launcher integration.
6. **Dedicated scheduling:** run archive sync, Knowledge capture, preparation,
   verifier execution, and observation with durable per-cycle status. The
   existing daemon schedules mirror, archive, and Knowledge jobs, but not the
   temporal runner or this isolated canary observer.

Until these are complete, the safe result is a structural shadow readiness
report with `release_claim=false`, not a production temporal-recall score.
